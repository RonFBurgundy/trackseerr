"""Import pipeline mixin and data structures for AcquisitionWorker."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from trackseerr import delay_gate
from trackseerr.acquisition_coordinator import _to_quality_profile
from trackseerr.clients.acquisition import is_torrent_driver_type
from trackseerr.mb_metadata_store import get_shared_enricher
from trackseerr.clients.plex import PlexClient
from trackseerr.media_servers import as_media_server
from trackseerr.download_roots import allowed_roots_for_client
from trackseerr.import_security import (
    clear_exec_bits,
    quarantine_files,
    verify_files,
)
from trackseerr.recycle_bin import (
    DisposeResult,
    effective_quarantine_path,
    restore_recycled,
)
from trackseerr.import_quality_check import CHECK_OFF, check_files, normalize_check_mode
from trackseerr.item_history import download_trigger_kwargs, emit
from trackseerr.library_health import record_weak_match
from trackseerr.library_monitoring import NATIVE_MONITOR_OPTIONS
from trackseerr.library import (
    build_tags_to_write,
    embed_album_artwork,
    inspect_audio_file,
    resolve_collision,
    write_audio_tags,
)
from trackseerr.models import (
    DownloadStatus,
    NotificationEvent,
    RequestStatus,
)
from trackseerr.naming import build_track_path
from trackseerr.notifications import notification_dispatcher
from trackseerr.redaction import safe_exc
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.storage import Database

from trackseerr.import_files import (
    _is_safe_cover_url,
    effective_import_mode,
    place_audio_file,
    prepare_file_for_tagging,
    translate_remote_path,
)
from trackseerr.seed_safety import (
    settle_transfer_after_import,
)
from trackseerr.track_matching import (
    MATCH_STRONG,
    _fingerprint_fallback_match,
    reconcile_audio_file_to_track_scored,
    resolve_download_expected_tracks,
)

logger = logging.getLogger(__name__)


def _quality_from_codec(meta: dict[str, Any]) -> Optional[str]:
    """Quality id for a file whose title said nothing, from its mutagen codec/bitrate (None when unrecognised)."""
    codec = str(meta.get("codec", "")).upper()
    bits = meta.get("bits_per_sample") or 16
    raw_br = meta.get("bitrate") or 320
    kbps = raw_br / 1000 if raw_br > 1000 else raw_br
    if codec == "FLAC":
        return "FLAC 24bit" if bits > 16 else "FLAC 16bit"
    if codec == "ALAC":
        return "ALAC"
    if codec in ("WAV", "AIFF"):
        return "WAV/AIFF"
    if codec == "OPUS":
        return "Opus"
    if codec in ("VORBIS", "OGG"):
        return "OGG Vorbis"
    if codec == "MP3":
        return "MP3 320" if kbps >= 310 else "MP3 192"
    if codec in ("AAC", "M4A"):
        return "AAC 256" if kbps >= 240 or not meta.get("bitrate") else "AAC (other)"
    return None


def _scan_monitor_option(media_settings: dict[str, Any]) -> str:
    """Monitor option for artists created by an import: the configured scan default, never the model default ``all``."""
    option = str(media_settings.get("scan_monitor_option") or "existing")
    return option if option in NATIVE_MONITOR_OPTIONS else "existing"


def record_import_events(
    db: Any,
    item: dict[str, Any],
    track_id: str,
    placed: Path,
    quality: Optional[str],
    meta: dict[str, Any],
    replaced_rows: list[dict[str, Any]],
) -> None:
    """``imported`` for a freshly linked file, plus ``upgraded`` (old -> new quality) when it replaced other files."""
    download_id = str(item.get("id") or "") or None
    provenance = download_trigger_kwargs(db, download_id)
    codec = meta.get("codec") or placed.suffix.lstrip(".").upper()
    common: dict[str, Any] = {
        "track_id": track_id, "request_id": item.get("request_id"), "download_id": download_id, **provenance,
    }
    emit(
        db, "imported", message=f"Imported {placed.name}",
        details={"path": str(placed), "quality": quality, "codec": codec, "release": item.get("title")}, **common,
    )
    if replaced_rows:
        old_quality = ", ".join(dict.fromkeys(str(r.get("quality_name") or "Unknown") for r in replaced_rows))
        emit(
            db, "upgraded", message=f"{old_quality} -> {quality}",
            details={"from_quality": old_quality, "to_quality": quality, "release": item.get("title")}, **common,
        )


@dataclass(frozen=True)
class _PollContext:
    """Settings and clients snapshot for a single acquisition poll pass."""

    media_settings: dict[str, Any]
    write_tags: bool
    embed_art: bool
    save_cover: bool
    plex_client: Optional[PlexClient]
    lidarr_mode: bool


@dataclass
class _ImportJob:
    """State across import pipeline phases for a single completed download."""

    item: dict[str, Any]
    download_id: Any
    client_id: Any
    client_config: dict[str, Any]
    driver: Any
    driver_type: str
    import_mode: str
    status_dict: dict[str, Any]
    target_lookup: Any
    audio_files: list[Path] = field(default_factory=list)
    probes: dict[str, Any] = field(default_factory=dict)
    req: Optional[dict[str, Any]] = None
    cover_bytes: Optional[bytes] = None
    target_album: Optional[dict[str, Any]] = None
    root_path: Optional[Path] = None
    expected_tracks: list[dict[str, Any]] = field(default_factory=list)
    remaining_expected_tracks: list[dict[str, Any]] = field(default_factory=list)
    imported_paths: list[str] = field(default_factory=list)
    held_files: list[str] = field(default_factory=list)
    held_msg: Optional[str] = None
    placed_to_track: dict[str, dict[str, Any]] = field(default_factory=dict)
    recycled_in_place: dict[str, tuple[DisposeResult, dict[str, Any]]] = field(default_factory=dict)
    last_metadata: dict[str, Any] = field(default_factory=dict)
    replaced_kept: list[str] = field(default_factory=list)
    replaced_retired: list[str] = field(default_factory=list)
    replacement_issue_id: Optional[str] = None
    target_summary: Optional[str] = None
    should_keep_seeding: bool = False



from trackseerr.acquisition_catalog import ImportCatalogMixin


class ImportPipelineMixin(ImportCatalogMixin):
    """Import pipeline mixin for AcquisitionWorker."""
    def _finalize_lidarr_item(
        self,
        db: Database,
        ctx: _PollContext,
        item: dict[str, Any],
        stats: dict[str, int],
    ) -> None:
        """Finalize and record availability for a Lidarr-managed download."""
        download_id = item["id"]
        plex_client = ctx.plex_client
        db.update_download_status(download_id, status=DownloadStatus.IMPORTED.value)
        req_row = None
        if item.get("request_id"):
            db.update_request_status(item["request_id"], RequestStatus.AVAILABLE.value)
            req_row = db.get_request(item["request_id"])
        stats["imported"] += 1
        try:
            db.record_event(
                "item_available",
                f"Imported '{item.get('title', '')}' to library",
                source="AcquisitionWorker",
                severity="info",
            )
        except Exception as ev_err:
            logger.warning("Failed to record Lidarr item_available event: %s", ev_err)
        try:
            notification_dispatcher.dispatch(
                NotificationEvent.ITEM_AVAILABLE,
                data={
                    "artist": item.get("artist"),
                    "title": item.get("title"),
                    "album": item.get("title") if item.get("item_type") == "album" else None,
                    "request_id": item.get("request_id"),
                    "download_id": download_id,
                    "cover_url": req_row.get("cover_url") if req_row else None,
                    "username": req_row.get("username") if req_row else None,
                    "user_id": req_row.get("user_id") if req_row else None,
                },
                db=db,
            )
        except Exception as ex:
            logger.warning("Failed to dispatch ITEM_AVAILABLE notification for Lidarr import: %s", ex)

        if plex_client:
            try:
                as_media_server(plex_client).refresh_library()
            except Exception as e:
                logger.warning("Error refreshing Plex after Lidarr import: %s", e)

    def _import_ready_item(
        self,
        db: Database,
        ctx: _PollContext,
        item: dict[str, Any],
        client_config: dict[str, Any],
        driver: Any,
        status_dict: dict[str, Any],
        stats: dict[str, int],
    ) -> None:
        """Import audio files for a completed download into the music library."""
        download_id = item["id"]
        client_id = item.get("client_id")
        target_lookup = item.get("download_hash") or download_id
        media_settings = ctx.media_settings

        stats["completed"] += 1
        db.update_download_status(download_id, status=DownloadStatus.IMPORTING.value)

        # Special case: Lidarr performs native file organization
        driver_type = str(client_config.get("driver_type", "")).lower()
        import_mode = effective_import_mode(driver_type, media_settings)
        if driver_type == "lidarr":
            self._finalize_lidarr_item(db, ctx, item, stats)
            return

        job = _ImportJob(
            item=item,
            download_id=download_id,
            client_id=client_id,
            client_config=client_config,
            driver=driver,
            driver_type=driver_type,
            import_mode=import_mode,
            status_dict=status_dict,
            target_lookup=target_lookup,
        )

        if not self._locate_audio_files(db, ctx, job, stats):
            return
        if not self._security_gate(db, ctx, job, stats):
            return
        if not self._bitrate_gate(db, ctx, job, stats):
            return
        self._prepare_placement(db, ctx, job)
        self._place_audio_files(db, ctx, job)
        if not self._check_placement(db, ctx, job, stats):
            return
        self._record_imported_files(db, ctx, job)
        self._upsert_native_catalog(db, ctx, job)
        self._complete_request(db, ctx, job)
        self._settle_download(db, ctx, job, stats)

    def _locate_audio_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Locate downloaded audio files with remote path translation."""
        mappings: list[dict[str, str]] = []
        extra_json = job.client_config.get("extra_settings_json")
        if extra_json:
            try:
                extra_data = json.loads(extra_json) if isinstance(extra_json, str) else extra_json
                if isinstance(extra_data, dict):
                    mappings = extra_data.get("remote_path_mappings", [])
            except (json.JSONDecodeError, TypeError):
                mappings = []

        raw_src = job.status_dict.get("source_path") or job.item.get("source_path")
        candidate_src = translate_remote_path(raw_src, mappings) if raw_src else None
        client_label = str(job.client_config.get("name") or job.client_id or "download client")
        self.allowed_roots = allowed_roots_for_client(db, ctx.media_settings, job.client_config, driver=job.driver)
        if not self.allowed_roots.roots:
            reason = "; ".join(self.allowed_roots.errors) or f"Could not read download folder from {client_label}"
            err_msg = f"{reason}; check client connection"
            logger.warning("Download %s cannot be imported yet: %s", job.download_id, err_msg)
            db.update_download_status(job.download_id, status=DownloadStatus.COMPLETED.value, error_message=err_msg)
            self.allowed_roots = None
            return False
        if candidate_src:
            ok, reject_reason = self.allowed_roots.check(candidate_src)
            if not ok:
                logger.warning("Rejecting source path %s from %s: %s", candidate_src, client_label, reject_reason)
                candidate_src = None

        search_term = job.item.get("title") or job.item.get("artist") or ""
        self._archive_errors = []
        job.audio_files = self._find_audio_files(candidate_src, search_term)

        if self._archive_errors and not job.audio_files:
            err_msg = "Archive rejected: " + "; ".join(self._archive_errors)
            try:
                db.record_event(
                    "import_security",
                    f"Archive limits exceeded for '{job.item.get('title', '')}': {err_msg}",
                    source="AcquisitionWorker",
                    severity="error",
                    details={"download_id": job.download_id, "archives": list(self._archive_errors)},
                )
            except sqlite3.Error as ev_err:
                logger.warning("Failed to record import_security event: %s", ev_err)
            db.update_download_status(job.download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add archive-rejected download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False

        if not job.audio_files:
            logger.warning(
                "Download %s marked completed but no audio files found at %s or download roots %s",
                job.download_id,
                candidate_src,
                ", ".join(str(r) for r in self._effective_roots().roots),
            )
            err_msg = "No audio files found for import in download staging"
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.FAILED.value,
                error_message=err_msg,
            )
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add missing-audio download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False
        return True

    def _security_gate(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Verify audio files against security checks, quarantining any offenders."""
        # Security gate (always on, any import_bitrate_check mode): magic bytes + header parse. One bad file
        # rejects the whole release; offenders are quarantined, never imported.
        job.probes = {}
        security = verify_files(job.audio_files, job.probes)
        if security.failed:
            err_msg = f"Security check failed: {security.reason()}"
            # Torrent sources keep seeding from their download folder, so they are copied, never moved. Usenet,
            # Soulseek and staging sources have nothing seeding and are moved out of the download folder.
            q_root = effective_quarantine_path(ctx.media_settings)
            keep_sources = is_torrent_driver_type(job.driver_type)
            if q_root is None:
                # No quarantine or library root is configured: never fall back to the process cwd.
                logger.warning(
                    "No quarantine folder or library root configured; leaving rejected files of download "
                    "%s in place (release is still refused).", job.download_id,
                )
                moved = []
            else:
                moved = quarantine_files(
                    [p for p, _ in security.failures], q_root, str(job.download_id), copy=keep_sources
                )
            try:
                db.record_event(
                    "import_security",
                    f"Security check failed for '{job.item.get('title', '')}': {security.reason()}",
                    source="AcquisitionWorker",
                    severity="error",
                    details={
                        "download_id": job.download_id,
                        "files": [{"file": p, "reason": r} for p, r in security.failures],
                        "quarantined_to": [str(m) for m in moved],
                        "sources_kept_for_seeding": keep_sources,
                    },
                )
            except sqlite3.Error as ev_err:
                logger.warning("Failed to record import_security event: %s", ev_err)
            db.record_download_item_event(
                "quarantined", str(job.download_id), message=f"Import security: {security.reason()}",
                details={
                    "release": job.item.get("title"),
                    "files": [{"file": p, "reason": r} for p, r in security.failures],
                    "quarantined_to": [str(m) for m in moved], "copied": keep_sources,
                },
            )
            logger.error("Import security failure for download %s: %s", job.download_id, err_msg)
            db.update_download_status(job.download_id, status=DownloadStatus.FAILED.value, error_message=err_msg)
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add security-rejected download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False
        return True

    def _bitrate_gate(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Validate audio file bitrates according to configured check mode."""
        # Per-track bitrate check (media management: import_bitrate_check = off | warn | reject).
        check_mode = normalize_check_mode(ctx.media_settings.get("import_bitrate_check"))
        if check_mode != CHECK_OFF:
            try:
                definitions = {str(d["quality"]): d for d in db.list_quality_definitions()}
                check = check_files(job.audio_files, check_mode, definitions, probes=job.probes)
            except Exception as chk_err:  # noqa: BLE001 - the check is advisory; it must never crash the worker loop
                logger.warning(
                    "Import bitrate check failed for download %s: %s: %s",
                    job.download_id,
                    type(chk_err).__name__,
                    chk_err,
                )
                check = None
            if check is not None and (check.out_of_range or check.skipped):
                summary = check.reason()
                try:
                    db.record_event(
                        "import_bitrate_check",
                        f"Bitrate check ({check_mode}) for '{job.item.get('title', '')}': {summary}",
                        source="AcquisitionWorker",
                        severity="error" if check.failed else "warning",
                        details={
                            "download_id": job.download_id,
                            "mode": check_mode,
                            "checked": check.checked,
                            "out_of_range": [
                                {
                                    "file": f.path,
                                    "quality": f.quality,
                                    "kbps": round(f.kbps, 1),
                                    "min_kbps": f.min_kbps,
                                    "max_kbps": f.max_kbps,
                                    "severity": f.severity,
                                    "detail": f.detail,
                                }
                                for f in check.out_of_range
                            ],
                            "skipped": [{"file": p, "reason": r} for p, r in check.skipped],
                        },
                    )
                except sqlite3.Error as ev_err:
                    logger.warning("Failed to record import_bitrate_check event: %s", ev_err)
                logger.warning("Import bitrate check (%s) for download %s: %s", check_mode, job.download_id, summary)
            if check is not None and check.failed:
                err_msg = f"Bitrate check failed: {check.reason()}"
                db.update_download_status(
                    job.download_id,
                    status=DownloadStatus.FAILED.value,
                    error_message=err_msg,
                )
                self._notify_download_failed(db, job.item, err_msg)
                try:
                    db.add_to_blocklist(
                        source_title=job.item.get("title", ""),
                        artist=job.item.get("artist"),
                        release_guid=job.item.get("id"),
                        info_hash=job.item.get("download_hash"),
                        reason=err_msg,
                        download_id=job.download_id,
                    )
                except Exception as bl_err:
                    logger.warning("Failed to add bitrate-rejected download to blocklist: %s", bl_err)
                stats["failed"] += 1
                return False
        return True

    def _prepare_placement(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Prepare target directory, metadata lookup, and cover art before placement."""
        # Organize and move each audio file
        job.imported_paths = []
        root_folder = ctx.media_settings.get("root_folder_path") or "/music"
        job.root_path = Path(root_folder).resolve()

        # Fetch associated request and album cover art if available
        job.req = db.get_request(job.item["request_id"]) if job.item.get("request_id") else None
        job.cover_bytes = None
        if job.req and (ctx.embed_art or ctx.save_cover):
            cover_url = job.req.get("cover_url")
            if cover_url and _is_safe_cover_url(cover_url):
                try:
                    resp = httpx.get(cover_url, timeout=10.0, follow_redirects=True)
                    if resp.status_code == 200 and resp.content:
                        job.cover_bytes = resp.content
                except httpx.HTTPError as e:
                    logger.warning("HTTP error fetching cover art from %s: %s", cover_url, e)
                except Exception as e:
                    logger.warning("Error fetching cover art from %s: %s", cover_url, e)
            elif cover_url:
                logger.warning("Cover art URL rejected by SSRF protection: %s", cover_url)

        job.last_metadata = {}

        # Check if item has album_id or matches an existing album in catalog
        job.target_album, job.expected_tracks = resolve_download_expected_tracks(db, job.item, job.req)

        job.remaining_expected_tracks = list(job.expected_tracks)
        job.placed_to_track = {}
        # placed path -> (recycle result, old file row) for old files recycled right before an in-place replace
        job.recycled_in_place = {}
        # Files with no catalog match when the release has expected tracks: left on disk for manual import.
        job.held_files = []

    def _place_one_file(  # noqa: C901, PLR0915
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        af: Path,
    ) -> None:
        """Place, tag, and enrich a single audio file."""
        assert job.root_path is not None
        try:
            metadata = inspect_audio_file(af)
        except Exception as e:
            logger.warning("Mutagen inspection failed for %s: %s; using item defaults", af, e)
            metadata = {
                "artist": job.item.get("artist", "Unknown Artist"),
                "title": job.item.get("title", af.stem),
                "album": job.item.get("title") if job.item.get("item_type") == "album" else "Unknown Album",
                "file_path": str(af),
                "extension": af.suffix.lower(),
                "track_number": None,
                "disc_number": 1,
                "total_discs": 1,
            }

        # Fallbacks for empty tags
        if not metadata.get("artist"):
            metadata["artist"] = job.item.get("artist") or "Unknown Artist"
        if not metadata.get("title"):
            metadata["title"] = job.item.get("title") or af.stem

        # Reconcile against expected catalog tracks if present
        matched_expected_track = None
        file_weak_strength: Optional[str] = None
        if job.remaining_expected_tracks:
            matched_expected_track, match_strength = reconcile_audio_file_to_track_scored(
                metadata, job.remaining_expected_tracks
            )
            tag_track = matched_expected_track
            matched_expected_track = _fingerprint_fallback_match(
                af, ctx.media_settings, job.remaining_expected_tracks, matched_expected_track, match_strength
            )
            if matched_expected_track is not None and matched_expected_track is tag_track and match_strength != MATCH_STRONG:
                file_weak_strength = match_strength
            if matched_expected_track:
                job.remaining_expected_tracks.remove(matched_expected_track)
                metadata["title"] = matched_expected_track["title"]
                metadata["track_number"] = int(matched_expected_track.get("track_number") or 1)
                metadata["disc_number"] = int(matched_expected_track.get("disc_number") or 1)
                if job.target_album:
                    metadata["album"] = job.target_album["title"]
                    art_cand = db.get_library_artist(job.target_album["artist_id"])
                    if art_cand:
                        metadata["artist"] = art_cand["name"]

        if job.expected_tracks and matched_expected_track is None:
            logger.warning(
                "Holding unmatched file %s for download %s (manual import required)", af, job.download_id
            )
            job.held_files.append(str(af))
            return

        # Disc 1 of a multi-disc release must use the multi-disc format too.
        known_discs = [int(metadata.get("total_discs") or 1)]
        known_discs += [int(t.get("disc_number") or 1) for t in job.expected_tracks]
        metadata["total_discs"] = max(known_discs)

        job.last_metadata = metadata

        target_str = build_track_path(metadata, ctx.media_settings)
        desired_path = Path(target_str).resolve()
        pre_recycled = self._recycle_in_place_target(
            db, ctx.media_settings, job.root_path, desired_path, matched_expected_track
        )
        final_target = desired_path if pre_recycled is not None else resolve_collision(target_str)
        target_path = Path(final_target).resolve()
        if not target_path.is_relative_to(job.root_path):
            logger.error("Destination %s escapes music root %s", target_path, job.root_path)
            if pre_recycled is not None:
                restore_recycled(pre_recycled[0])
            return

        try:
            placed_path = place_audio_file(af, target_path, mode=job.import_mode)
        except Exception:
            if pre_recycled is not None:
                restore_recycled(pre_recycled[0])  # the replacement never landed: put the old bytes back
            raise
        if pre_recycled is not None:
            # The old file's bytes now live in the recycle bin; its row would point at the new file.
            job.recycled_in_place[str(placed_path)] = pre_recycled
        job.imported_paths.append(str(placed_path))
        if matched_expected_track:
            job.placed_to_track[str(placed_path)] = matched_expected_track
            if file_weak_strength is not None:
                record_weak_match(
                    db,
                    str(placed_path),
                    track_id=str(matched_expected_track.get("id") or ""),
                    title=str(matched_expected_track.get("title") or ""),
                    source_name=str(job.item.get("title") or ""),
                    strength=file_weak_strength,
                )
        logger.info("Successfully imported '%s' -> '%s'", af.name, placed_path)
        try:
            db.record_event(
                "item_available",
                f"Imported '{af.name}' to library",
                source="AcquisitionWorker",
                severity="info",
            )
        except Exception as ev_err:
            logger.warning("Failed to record item_available event: %s", ev_err)

        # Tag writing and artwork embedding
        tags_to_write: dict[str, Any] = build_tags_to_write(
            metadata,
            req=job.req,
            audio_files_count=len(job.audio_files),
        )

        # Asynchronously enrich with MBIDs if enabled
        if ctx.media_settings.get("enrich_mbids", True):
            try:
                enricher = get_shared_enricher(db)
                artist_query = str(tags_to_write.get("artist") or "")
                album_query = str(tags_to_write.get("album") or "")
                title_query = str(tags_to_write.get("title") or "")
                track_isrc = metadata.get("isrc")
                resolved_mbids = enricher.lookup_track_mbids(
                    artist_query, album_query, title_query, isrc=track_isrc
                )
                if resolved_mbids:
                    for mb_key in (
                        "musicbrainz_artistid",
                        "musicbrainz_albumid",
                        "musicbrainz_releasegroupid",
                        "musicbrainz_trackid",
                    ):
                        if resolved_mbids.get(mb_key):
                            tags_to_write[mb_key] = resolved_mbids[mb_key]

                if not job.cover_bytes and (ctx.embed_art or ctx.save_cover):
                    rg_id = resolved_mbids.get("musicbrainz_releasegroupid") if resolved_mbids else None
                    rel_id = resolved_mbids.get("musicbrainz_albumid") if resolved_mbids else None
                    resolved_cover_url = enricher.get_cover_art_url(release_group_id=rg_id, release_id=rel_id)
                    if resolved_cover_url and _is_safe_cover_url(resolved_cover_url):
                        try:
                            resp = httpx.get(resolved_cover_url, timeout=5.0, follow_redirects=True)
                            if resp.status_code == 200 and resp.content:
                                job.cover_bytes = resp.content
                        except Exception as exc:
                            logger.debug("Failed fetching cover art from Cover Art Archive %s: %s", resolved_cover_url, exc)
            except Exception as e:
                logger.warning("AcquisitionWorker: MBID enrichment error: %s", e)

        file_write_tags, file_embed_art = ctx.write_tags, ctx.embed_art
        if (file_write_tags or (file_embed_art and job.cover_bytes)) and not prepare_file_for_tagging(placed_path, ctx.media_settings):
            file_write_tags = file_embed_art = False
        if file_write_tags:
            art_to_embed = job.cover_bytes if file_embed_art else None
            try:
                write_audio_tags(placed_path, tags=tags_to_write, cover_art_bytes=art_to_embed)
            except Exception as e:
                logger.warning("Error writing audio tags to %s: %s", placed_path, e)
        elif file_embed_art and job.cover_bytes:
            try:
                embed_album_artwork(placed_path, job.cover_bytes)
            except Exception as e:
                logger.warning("Error embedding artwork into %s: %s", placed_path, e)

        if ctx.save_cover and job.cover_bytes:
            cover_file = placed_path.parent / "cover.jpg"
            if not cover_file.exists():
                try:
                    cover_file.write_bytes(job.cover_bytes)
                    clear_exec_bits(cover_file)
                    logger.info("Saved album cover to %s", cover_file)
                except OSError as e:
                    logger.warning("Failed to save cover.jpg at %s: %s", cover_file, e)

    def _place_audio_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Place each audio file into the destination library folder."""
        for af in job.audio_files:
            self._place_one_file(db, ctx, job, af)

    def _check_placement(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Check placed files against held/error conditions after placement pass."""
        if job.held_files:
            db.set_download_unmatched_files(job.download_id, job.held_files)
            job.held_msg = f"{len(job.held_files)} file(s) couldn't be matched — manual import required"
            if not job.imported_paths:
                # Nothing placed: park the download for manual import (not a failure, no blocklisting).
                db.update_download_status(
                    job.download_id, status=DownloadStatus.WARNING.value, error_message=job.held_msg
                )
                return False

        if not job.imported_paths:
            logger.error("No audio files were successfully imported for download %s", job.download_id)
            err_msg = "Destination escaped music root or placement failed"
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.FAILED.value,
                error_message=err_msg,
            )
            self._notify_download_failed(db, job.item, err_msg)
            try:
                db.add_to_blocklist(
                    source_title=job.item.get("title", ""),
                    artist=job.item.get("artist"),
                    release_guid=job.item.get("id"),
                    info_hash=job.item.get("download_hash"),
                    reason=err_msg,
                    download_id=job.download_id,
                )
            except Exception as bl_err:
                logger.warning("Failed to add unplaced download to blocklist: %s", bl_err)
            stats["failed"] += 1
            return False
        return True

    def _complete_request(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Update request status and evaluate quality cutoff for completed request."""
        if job.item.get("request_id"):
            db.update_request_status(job.item["request_id"], RequestStatus.AVAILABLE.value)
            try:
                parsed = parse_release_title(job.item.get("title") or "")
                if parsed.quality == "Unknown" and job.last_metadata:
                    parsed.quality = _quality_from_codec(job.last_metadata) or parsed.quality

                profile_dict = None
                if job.req and job.req.get("quality_profile_id"):
                    profile_dict = db.get_quality_profile(job.req["quality_profile_id"])
                if not profile_dict:
                    profile_dict = db.get_default_quality_profile()

                if profile_dict:
                    profile = _to_quality_profile(profile_dict)
                    eval_res = evaluate_release(
                        release=parsed,
                        profile=profile,
                        size_bytes=job.item.get("size_bytes"),
                        artist_tags=delay_gate.artist_tags(
                            db, (job.req.get("artist") if job.req else None) or job.item.get("artist")
                        ),
                    )
                    current_q = eval_res.parsed_quality
                    cutoff_met_val = 1 if eval_res.meets_cutoff else 0
                    db.update_request_quality(
                        job.item["request_id"],
                        current_quality=current_q,
                        cutoff_met=cutoff_met_val,
                    )
            except Exception as ex:
                logger.warning("Error evaluating release quality for request %s: %s", job.item.get("request_id"), ex)

    def _settle_download(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> None:
        """Decide download settling, seeding retention, issue comments, notifications, and Plex refresh."""
        job.should_keep_seeding = False
        # Held files still live in the client's download folder: never remove the transfer while they wait.
        if job.held_files:
            logger.info(
                "Download %s keeps %d unmatched file(s); skipping download-client cleanup",
                job.download_id,
                len(job.held_files),
            )
        else:
            try:
                db.set_download_placed_files(job.download_id, job.imported_paths, job.import_mode)
            except sqlite3.Error as placed_err:
                logger.warning("Could not record placed files for %s: %s", job.download_id, safe_exc(placed_err))
            job.should_keep_seeding = (
                settle_transfer_after_import(
                    job.driver,
                    job.target_lookup,
                    ctx.media_settings,
                    job.import_mode,
                    job.status_dict,
                    db.get_active_download(job.download_id) or job.item,
                    db,
                )
                == DownloadStatus.COMPLETED.value
            )

        if job.held_files:
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.WARNING.value,
                error_message=job.held_msg,
                target_path=job.target_summary,
            )
        elif job.should_keep_seeding:
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.COMPLETED.value,
                target_path=job.target_summary,
            )
        else:
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.IMPORTED.value,
                target_path=job.target_summary,
            )

        stats["imported"] += 1

        try:  # a grab made by an issue's "Search again" tells that issue; the admin decides the status
            issue_id = job.replacement_issue_id
            if issue_id and db.get_issue(issue_id):
                body = "Replacement imported"
                for line in job.replaced_retired:
                    body += f"\nRetired old file: {line}"
                for line in job.replaced_kept:
                    body += f"\nOld file kept at {line}"
                db.add_issue_comment(issue_id, None, body, is_admin=True, is_system=True, staff=True)
        except sqlite3.Error as issue_err:
            logger.warning("Could not comment on the issue for download %s: %s", job.download_id, safe_exc(issue_err))

        try:
            notification_dispatcher.dispatch(
                NotificationEvent.ITEM_AVAILABLE,
                data={
                    "artist": job.item.get("artist"),
                    "title": job.item.get("title"),
                    "album": job.item.get("title") if job.item.get("item_type") == "album" else None,
                    "request_id": job.item.get("request_id"),
                    "download_id": job.download_id,
                    "target_path": job.target_summary,
                    "cover_url": job.req.get("cover_url") if job.req else None,
                    "username": job.req.get("username") if job.req else None,
                    "user_id": job.req.get("user_id") if job.req else None,
                },
                db=db,
            )
        except Exception as ex:
            logger.warning("Failed to dispatch ITEM_AVAILABLE notification for native import: %s", ex)

        # Trigger Plex library refresh ping
        if ctx.plex_client:
            try:
                as_media_server(ctx.plex_client).refresh_library()
            except Exception as e:
                logger.warning("Error triggering Plex library refresh: %s", e)



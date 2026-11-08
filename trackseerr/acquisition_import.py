"""Import pipeline mixin and data structures for AcquisitionWorker."""

from __future__ import annotations

import json
import logging
import re
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
from trackseerr.media_servers.refresh_scheduler import request_library_refresh
from trackseerr.download_folder_cleanup import prune_empty_parents
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
from trackseerr.track_credits import tag_artist_from_credits
from trackseerr.models import (
    DownloadStatus,
    NotificationEvent,
    RequestStatus,
)
from trackseerr.naming import build_track_path
from trackseerr.artist_folders import resolve_artist_folder
from trackseerr.track_credits import naming_credits
from trackseerr.notifications import notification_dispatcher
from trackseerr.redaction import safe_exc
from trackseerr.lidarr_release import norm_title, titles_match
from trackseerr.quality import QualityProfile, evaluate_release, parse_release_title
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
    _effective_file_title,
    _fingerprint_fallback_match,
    candidate_artist_ids,
    download_artist_id,
    find_fileless_equivalent,
    find_owned_equivalent,
    find_owned_for_item,
    reconcile_audio_file_to_track_scored,
    resolve_download_expected_tracks,
    same_song_title,
    wanted_song,
)

logger = logging.getLogger(__name__)


def _apply_catalog_album(
    metadata: dict[str, Any],
    album: dict[str, Any],
    artist: Optional[dict[str, Any]],
    media_settings: Optional[dict[str, Any]] = None,
    db: Optional[Database] = None,
) -> None:
    """Re-labels a catalog-matched file with the catalog album/artist so compilation tags can't misfile it.

    The artist's existing folder (when ``rename_artist_folders`` is off) is kept, so a new album lands beside the old ones.
    """
    metadata["album"] = album["title"]
    if artist:
        metadata["artist"] = artist["name"]
        metadata["album_artist"] = artist["name"]
        metadata["artist_folder"] = resolve_artist_folder(db, artist, media_settings or {})
    if album.get("year"):
        metadata["release_year"] = metadata["year"] = album["year"]
    else:
        for key in ("release_year", "year"):
            if key in metadata and metadata[key] in ("", None):
                del metadata[key]
    is_compilation = str(album.get("album_type") or "").lower() == "compilation"
    metadata["is_compilation"] = metadata["compilation"] = is_compilation
    # Only a catalog compilation keeps an album type: a plain type would add a " - album" folder suffix, and a stale
    # compilation type from the file's tags would still trigger the Various Artists layout.
    if is_compilation:
        metadata["album_type"] = "compilation"
    else:
        metadata.pop("album_type", None)


def _existing_folder_for_tags(
    db: Database,
    metadata: dict[str, Any],
    media_settings: dict[str, Any],
) -> Optional[str]:
    """Finds existing library artist folder for uncatalogued track tags when rename_artist_folders is off."""
    if media_settings.get("rename_artist_folders"):
        return None
    if metadata.get("is_compilation"):
        return None
    try:
        artist = None
        mbid = metadata.get("musicbrainz_albumartistid") or metadata.get("musicbrainz_artistid")
        if mbid:
            artist = db.get_library_artist_by_mbid(str(mbid))
        if not artist:
            name = metadata.get("album_artist") or metadata.get("artist")
            if name:
                name_str = str(name)
                artist = db.get_library_artist_by_name(name_str)
                if not artist and "&" in name_str:
                    artist = db.get_library_artist_by_name(name_str.replace("&", "and"))
                if not artist and re.search(r"\band\b", name_str, flags=re.IGNORECASE):
                    artist = db.get_library_artist_by_name(re.sub(r"\band\b", "&", name_str, flags=re.IGNORECASE))
        if artist:
            return resolve_artist_folder(db, artist, media_settings)
    except sqlite3.Error as e:
        logger.warning("Failed to lookup existing artist folder for tags: %s", e)
    return None



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
    details: dict[str, Any] = {
        "path": str(placed),
        "quality": quality,
        "codec": codec,
        "release": item.get("title"),
    }
    skipped = item.get("skipped_files")
    if skipped:
        details["skipped_other_files"] = len(skipped)
        details["note"] = f"Skipped {len(skipped)} other files in {item.get('title')}: not the wanted song"
    emit(
        db, "imported", message=f"Imported {placed.name}",
        details=details, **common,
    )
    if replaced_rows:
        old_quality = ", ".join(dict.fromkeys(str(r.get("quality_name") or "Unknown") for r in replaced_rows))
        emit(
            db, "upgraded", message=f"{old_quality} -> {quality}",
            details={"from_quality": old_quality, "to_quality": quality, "release": item.get("title")}, **common,
        )


_download_artist_id = download_artist_id


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
    db: Optional[Database] = None
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
    duplicate_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    held_msg: Optional[str] = None
    placed_to_track: dict[str, dict[str, Any]] = field(default_factory=dict)
    recycled_in_place: dict[str, tuple[DisposeResult, dict[str, Any]]] = field(default_factory=dict)
    last_metadata: dict[str, Any] = field(default_factory=dict)
    replaced_kept: list[str] = field(default_factory=list)
    replaced_retired: list[str] = field(default_factory=list)
    replacement_issue_id: Optional[str] = None
    target_summary: Optional[str] = None
    should_keep_seeding: bool = False
    duplicate_owned_title: Optional[str] = None
    duplicate_owned_track_id: Optional[str] = None
    selected_track_match: Optional[tuple[Path, dict[str, Any]]] = None


_LEADING_TRACK_DISC_RE = re.compile(
    r"^(?:(?:\d{1,3}\s*[-._]\s*)?\d{1,3}|\d{1,4})\s*[-._\s]\s*",
    re.IGNORECASE,
)
_SINGLE_VARIANT_RE = re.compile(
    r"(?:[\(\[]\s*(?:(?:original\s+)?single\s+version|(?:radio|single)\s+edit|(?:radio|single)\s+version|single)\s*[\)\]]|\b(?:single\s+version|single\s+edit|radio\s+edit|radio\s+version)\b)",
    re.IGNORECASE,
)


def _strip_leading_track_number(stem: str) -> str:
    cleaned = _LEADING_TRACK_DISC_RE.sub("", stem).strip()
    return cleaned if cleaned else stem


def _strip_artist_prefix(title: str, artist: Optional[str]) -> str:
    if not artist:
        return title
    art_clean = artist.strip().casefold()
    t_clean = title.strip()
    if t_clean.casefold().startswith(art_clean):
        remainder = t_clean[len(art_clean):].lstrip(" -–—_.")
        if remainder:
            return remainder
    return title


def _candidate_file_title(af: Path, meta: dict[str, Any], artist: Optional[str] = None) -> str:
    eff = _effective_file_title(meta) if meta else None
    if eff and str(eff).strip():
        tag_title = str(eff).strip()
    else:
        tag_title = str(meta.get("title") or "").strip()
    if tag_title:
        cleaned_tag = _strip_leading_track_number(tag_title)
        if artist:
            cleaned_tag = _strip_artist_prefix(cleaned_tag, artist)
        return cleaned_tag
    stem = af.stem
    cleaned = _strip_leading_track_number(stem)
    if artist:
        cleaned = _strip_artist_prefix(cleaned, artist)
    return cleaned


def _resolve_wanted_track(
    db: Database,
    item: dict[str, Any],
    req: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Resolves a catalog track row for a wanted download item without creating any new rows."""
    if item.get("track_id"):
        trk = db.get_library_track(str(item["track_id"]))
        if trk:
            return trk
    if req and req.get("track_id"):
        trk = db.get_library_track(str(req["track_id"]))
        if trk:
            return trk
    rec_id = (req.get("mb_recording_id") if req else None) or item.get("mb_recording_id")
    if rec_id:
        trk = db.get_library_track_by_mb_recording_id(str(rec_id))
        if trk:
            return trk

    artist_name = (req.get("artist") if req else None) or item.get("artist")
    track_title = (
        (req.get("title") if req else None)
        or (item.get("title") if item.get("item_type") == "track" else None)
    )
    if artist_name and track_title:
        aids = candidate_artist_ids(db, item, artist_name)
        for aid in aids:
            if not aid:
                continue
            for t in db.list_fileless_tracks_for_artist(aid):
                if titles_match(t.get("title"), track_title):
                    return t
            for t in db.list_filed_tracks_for_artist(aid):
                if titles_match(t.get("title"), track_title):
                    return t
    return None


def _file_quality_key(
    path: Path, meta: dict[str, Any], profile: Optional[QualityProfile]
) -> tuple[int, int, int, int, str]:
    """Sort key for quality comparison: best file has the lowest key tuple."""
    quality_name = _quality_from_codec(meta) or ""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    ext = path.suffix.lower()
    is_lossless = ext in (".flac", ".alac", ".wav", ".aiff") or "FLAC" in quality_name.upper()

    if not quality_name:
        if ext == ".flac":
            quality_name = "FLAC 16bit"
        elif ext == ".mp3":
            quality_name = "MP3 320"
        elif ext in (".m4a", ".aac"):
            quality_name = "AAC 256"

    allowed_rank = 0
    tier_rank = 0 if is_lossless else 10
    if profile and quality_name:
        res = evaluate_release(parse_release_title(quality_name), profile)
        if not res.is_acceptable:
            allowed_rank = 1
            tier_rank = 999999
        elif res.tier is not None:
            tier_rank = res.tier

    return (allowed_rank, tier_rank, 0 if is_lossless else 1, -size, str(path))


def _resolve_job_wanted_track_and_title(
    job: _ImportJob,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Resolves catalog track and returns (wanted_track, wanted_title) for the import job without mutating job."""
    db = job.db
    resolved_track = None
    if db:
        resolved_track = _resolve_wanted_track(db, job.item, job.req)

    if resolved_track and resolved_track.get("title"):
        return resolved_track, str(resolved_track["title"])
    if job.item.get("track_id") and db:
        trk = db.get_library_track(str(job.item["track_id"]))
        if trk and trk.get("title"):
            return trk, str(trk["title"])
    if job.req and job.req.get("title"):
        return resolved_track, str(job.req["title"])
    if job.item.get("title"):
        return resolved_track, str(job.item["title"])
    return resolved_track, None


def _match_files_for_track(
    audio_files: list[Path],
    wanted_title: str,
    artist: Optional[str],
) -> list[tuple[Path, dict[str, Any]]]:
    """Matches files against wanted title, preferring exact plain matches over single version variants."""
    wanted_has_single_variant = bool(_SINGLE_VARIANT_RE.search(wanted_title))
    tier1_plain: list[tuple[Path, dict[str, Any]]] = []
    tier2_variant: list[tuple[Path, dict[str, Any]]] = []

    for af in audio_files:
        try:
            meta = inspect_audio_file(af)
        except (ValueError, OSError, KeyError) as insp_err:
            logger.debug("Inspection error during file selection for %s: %s", af, insp_err)
            meta = {}
        cand_title = _candidate_file_title(af, meta, artist)
        matches = titles_match(cand_title, wanted_title)
        if not matches and artist:
            cand_stripped = _strip_artist_prefix(cand_title, artist)
            matches = titles_match(cand_stripped, wanted_title)

        if not matches:
            continue

        cand_has_single_variant = bool(
            _SINGLE_VARIANT_RE.search(cand_title) or _SINGLE_VARIANT_RE.search(af.stem)
        )
        if wanted_has_single_variant or not cand_has_single_variant:
            tier1_plain.append((af, meta))
        else:
            tier2_variant.append((af, meta))

    return tier1_plain if tier1_plain else tier2_variant


def _job_quality_profile(job: _ImportJob) -> Optional[QualityProfile]:
    """Retrieves the effective QualityProfile for the job."""
    db = job.db
    if not db:
        return None
    profile_dict = None
    if job.req and job.req.get("quality_profile_id"):
        profile_dict = db.get_quality_profile(job.req["quality_profile_id"])
    profile_dict = profile_dict or db.get_default_quality_profile()
    return _to_quality_profile(profile_dict) if profile_dict else None


def _record_skipped_files(job: _ImportJob, skipped: list[Path]) -> None:
    """Logs and records event for files skipped during track selection."""
    release_title = str(job.item.get("title") or job.download_id)
    logger.info("Skipped %d other files in %s: not the wanted song", len(skipped), release_title)
    job.skipped_files = [str(f) for f in skipped]
    job.item["skipped_files"] = job.skipped_files
    if job.db:
        try:
            job.db.record_event(
                "skipped_files",
                f"Skipped {len(skipped)} other files in '{release_title}': not the wanted song",
                source="AcquisitionWorker",
                severity="info",
            )
        except sqlite3.Error as ev_err:
            logger.warning("Failed to record skipped_files event: %s", ev_err)


def _parse_job_selected_files(item: dict[str, Any]) -> Optional[list[Any]]:
    """Extracts and parses selected_files from a download item if present."""
    raw = item.get("selected_files")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else None
        except (ValueError, TypeError):
            return None
    return None


def _select_files_for_track(job: _ImportJob) -> list[Path]:
    """Selects the single best audio file for a track-level download item."""
    if not job.audio_files:
        return []

    wanted_track, wanted_title = _resolve_job_wanted_track_and_title(job)
    if not wanted_title:
        return job.audio_files

    artist = job.item.get("artist") or (job.req.get("artist") if job.req else None)
    candidate_pool = _match_files_for_track(job.audio_files, wanted_title, artist)

    if not candidate_pool:
        if len(job.audio_files) <= 1:
            if wanted_track and job.audio_files:
                job.selected_track_match = (job.audio_files[0], wanted_track)
                if not job.item.get("track_id"):
                    job.item["track_id"] = str(wanted_track["id"])
                if not job.target_album and wanted_track.get("album_id") and job.db:
                    job.target_album = job.db.get_library_album(str(wanted_track["album_id"]))
            return job.audio_files
        job.held_files = [str(f) for f in job.audio_files]
        job.held_msg = f'No file in the download matches "{wanted_title}"'
        if job.db:
            job.db.set_download_unmatched_files(job.download_id, job.held_files)
            job.db.update_download_status(
                job.download_id,
                status=DownloadStatus.WARNING.value,
                error_message=job.held_msg,
            )
        return []

    profile = _job_quality_profile(job)
    best_file, _ = min(
        candidate_pool,
        key=lambda pair: _file_quality_key(pair[0], pair[1], profile),
    )

    skipped = [f for f in job.audio_files if f != best_file]
    if skipped:
        _record_skipped_files(job, skipped)

    if wanted_track:
        job.selected_track_match = (best_file, wanted_track)
        if not job.item.get("track_id"):
            job.item["track_id"] = str(wanted_track["id"])
        if not job.target_album and wanted_track.get("album_id") and job.db:
            job.target_album = job.db.get_library_album(str(wanted_track["album_id"]))

    return [best_file]


def _select_files_for_album(job: _ImportJob) -> list[Path]:
    """For an album grab with dual formats per track, keeps only the best format per track."""
    if len(job.audio_files) <= 1:
        return job.audio_files

    artist = job.item.get("artist") or (job.req.get("artist") if job.req else None)
    file_records: list[tuple[Path, dict[str, Any], Any]] = []

    for af in job.audio_files:
        try:
            meta = inspect_audio_file(af)
        except (ValueError, OSError, KeyError) as insp_err:
            logger.debug("Album inspection error for %s: %s", af, insp_err)
            meta = {}
        cand_title = _candidate_file_title(af, meta, artist)
        disc_num = meta.get("disc_number") or 1
        track_num = meta.get("track_number")
        if track_num is None:
            m = re.match(r"^\s*(\d{1,3})", af.stem)
            if m:
                track_num = int(m.group(1))

        if track_num is not None:
            key = (int(disc_num), int(track_num), norm_title(cand_title))
        else:
            key = (int(disc_num), 0, norm_title(cand_title))

        file_records.append((af, meta, key))

    groups: dict[Any, list[tuple[Path, dict[str, Any]]]] = {}
    for af, meta, key in file_records:
        groups.setdefault(key, []).append((af, meta))

    if all(len(g) == 1 for g in groups.values()):
        return job.audio_files

    profile = _job_quality_profile(job)

    selected: list[Path] = []
    skipped_count = 0
    for key, members in groups.items():
        formats = {pair[0].suffix.lower() for pair in members}
        if len(members) == 1 or len(formats) == 1:
            selected.extend(pair[0] for pair in members)
        else:
            best_af, _ = min(
                members,
                key=lambda pair: _file_quality_key(pair[0], pair[1], profile),
            )
            selected.append(best_af)
            skipped_count += len(members) - 1

    if skipped_count:
        logger.info(
            "Album grab %s has dual formats: kept best format for %d tracks (%d files skipped)",
            job.download_id,
            len(groups),
            skipped_count,
        )

    selected_set = set(selected)
    return [f for f in job.audio_files if f in selected_set]



from trackseerr.acquisition_catalog import ImportCatalogMixin


class ImportPipelineMixin(ImportCatalogMixin):
    """Import pipeline mixin for AcquisitionWorker."""

    _select_files_for_track = staticmethod(_select_files_for_track)
    _select_files_for_album = staticmethod(_select_files_for_album)

    def _filter_download_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Filter files for track-level or album-level downloads before security and placement."""
        if job.item.get("request_id") and not job.req:
            job.req = db.get_request(job.item["request_id"])

        effective_type = (job.req.get("item_type") if job.req else None) or job.item.get("item_type") or "track"

        if effective_type == "track":
            selected = self._select_files_for_track(job)
            if not selected:
                return False
            job.audio_files = selected
        elif effective_type == "album":
            selected = self._select_files_for_album(job)
            job.audio_files = selected

        return True

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
            request_library_refresh(as_media_server(plex_client), reason="Lidarr import")

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
            db=db,
        )

        if not self._locate_audio_files(db, ctx, job, stats):
            return
        if not self._security_gate(db, ctx, job, stats):
            return
        if not self._filter_download_files(db, ctx, job, stats):
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

    def _locate_selected_files(
        self,
        db: Database,
        job: _ImportJob,
        candidate_src: Optional[str],
        selected_files: list[Any],
    ) -> bool:
        """Locates only the files explicitly selected for a partial pack download."""
        if not candidate_src:
            logger.warning("Download %s has selected_files but candidate_src is missing", job.download_id)
            db.update_download_status(
                job.download_id,
                status=DownloadStatus.WARNING.value,
                error_message="Selected file not found: missing source path",
            )
            return False

        base_dir = Path(candidate_src)
        found: list[Path] = []
        for item in selected_files:
            rel = item.get("path") if isinstance(item, dict) else str(item)
            rel_clean = str(rel).replace("\\", "/").lstrip("/")
            rel_path = Path(rel_clean)
            filename = rel_path.name

            candidate_paths = [
                base_dir / rel_clean,
                base_dir.parent / rel_clean,
                base_dir / filename,
            ]
            parts = rel_clean.split("/")
            if len(parts) > 1 and base_dir.name == parts[0]:
                candidate_paths.append(base_dir / "/".join(parts[1:]))

            matched = None
            for cp in candidate_paths:
                if cp.is_file():
                    matched = cp.resolve()
                    break

            if not matched:
                logger.warning("Download %s selected file not found: %s", job.download_id, filename)
                db.update_download_status(
                    job.download_id,
                    status=DownloadStatus.WARNING.value,
                    error_message=f"Selected file not found: {filename}",
                )
                return False
            found.append(matched)

        job.audio_files = sorted(dict.fromkeys(found))
        return True

    def _resolve_candidate_source(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> Optional[str]:
        """Resolves and validates download source path against allowed roots."""
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
            return None
        if candidate_src:
            ok, reject_reason = self.allowed_roots.check(candidate_src)
            if not ok:
                logger.warning("Rejecting source path %s from %s: %s", candidate_src, client_label, reject_reason)
                candidate_src = None
        return candidate_src

    def _locate_audio_files(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        stats: dict[str, int],
    ) -> bool:
        """Locate downloaded audio files with remote path translation."""
        candidate_src = self._resolve_candidate_source(db, ctx, job)
        if candidate_src is None and not self.allowed_roots:
            return False

        selected_files = _parse_job_selected_files(job.item)
        if selected_files:
            return self._locate_selected_files(db, job, candidate_src, selected_files)

        err_msg_orig = str(job.item.get("error_message") or "")
        held_unmatched = job.item.get("unmatched_files") or []
        if err_msg_orig.startswith("Retried import") and held_unmatched:
            existing = [Path(p).resolve() for p in held_unmatched if Path(p).is_file()]
            if not existing:
                db.update_download_status(
                    job.download_id,
                    status=DownloadStatus.WARNING.value,
                    error_message="Retried import: file no longer on disk",
                )
                return False
            job.audio_files = sorted(dict.fromkeys(existing))
        else:
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
        job.duplicate_files = []

    def _cross_album_match(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
        metadata: dict[str, Any],
        duration: Optional[float],
    ) -> Optional[dict[str, Any]]:
        """Matches an unplaced file to an empty track on another album of candidate artists."""
        title = metadata.get("title") or ""
        alt = find_fileless_equivalent(
            db,
            candidate_artist_ids(db, job.item, metadata.get("artist")),
            title,
            duration,
            isrc=metadata.get("isrc"),
        )
        if not alt:
            return None
        logger.info(
            "Download %s: %s matches %s on another album; importing there",
            job.download_id,
            title,
            alt.get("title"),
        )
        metadata["title"] = alt["title"]
        metadata["track_number"] = int(alt.get("track_number") or 1)
        metadata["disc_number"] = int(alt.get("disc_number") or 1)
        alt_album = db.get_library_album(str(alt["album_id"]))
        alt_artist = db.get_library_artist(str(alt["artist_id"])) if alt.get("artist_id") else None
        if alt_album:
            _apply_catalog_album(
                metadata,
                alt_album,
                alt_artist,
                ctx.media_settings,
                db=db,
            )
        return alt

    @staticmethod
    def _prefer_wanted_track(
        job: _ImportJob,
        metadata: dict[str, Any],
        matched: Optional[dict[str, Any]],
        strength: str,
    ) -> tuple[Optional[dict[str, Any]], str]:
        """A single-track grab whose file is titled like the wanted track lands on the wanted track, not a neighbour.

        An unmatched file stays unmatched: it goes through the owned-elsewhere / hold rules, never onto the wanted row.
        """
        wanted_id = str(job.item.get("track_id") or "")
        if not wanted_id or matched is None or str(matched.get("id") or "") == wanted_id:
            return matched, strength
        file_title = _effective_file_title(metadata)
        for t in job.remaining_expected_tracks:
            if str(t.get("id") or "") == wanted_id and titles_match(t.get("title") or "", file_title):
                return t, MATCH_STRONG
        return matched, strength

    @staticmethod
    def _is_strict_upgrade(
        db: Database, job: _ImportJob, existing: dict[str, Any], metadata: dict[str, Any]
    ) -> bool:
        """True when the new file may replace the one on disk for the download's own track.

        An explicit issue replacement always may. When the new file's quality or the quality profile cannot be
        evaluated there is nothing to compare, so the grab replaces as it always did; otherwise the new file must
        rank strictly better than the old one.
        """
        try:
            if db.get_download_replacement_issue(str(job.download_id)):
                return True
        except sqlite3.Error as exc:
            logger.warning("Could not look up replacement issue for %s: %s", job.download_id, safe_exc(exc))
        new_quality = _quality_from_codec(metadata)
        old_quality = str(existing.get("quality_name") or "")
        if not new_quality:
            return True
        profile_dict = None
        if job.req and job.req.get("quality_profile_id"):
            profile_dict = db.get_quality_profile(job.req["quality_profile_id"])
        profile_dict = profile_dict or db.get_default_quality_profile()
        if not profile_dict:
            return True
        profile = _to_quality_profile(profile_dict)
        new_res = evaluate_release(parse_release_title(new_quality), profile)
        if new_res.tier is None or not new_res.is_acceptable:
            return False
        old_res = evaluate_release(parse_release_title(old_quality), profile)
        return old_res.tier is None or new_res.tier < old_res.tier

    def _existing_file_blocks_placement(
        self, db: Database, job: _ImportJob, af: Path, metadata: dict[str, Any], matched: dict[str, Any]
    ) -> bool:
        """True (and the file is booked as a duplicate) when ``matched`` already has a file this one must not replace."""
        track_id = str(matched.get("id") or "")
        existing = db.get_library_file_for_track(track_id) if track_id else None
        if existing is None:
            return False
        wanted_id = str(job.item.get("track_id") or "")
        own_track = not wanted_id or track_id == wanted_id
        if own_track and self._is_strict_upgrade(db, job, existing, metadata):
            return False
        logger.info(
            "Download %s: %s would replace the file already on '%s'; discarding as duplicate",
            job.download_id,
            af.name,
            matched.get("title"),
        )
        job.duplicate_files.append(str(af))
        if not job.duplicate_owned_title and matched.get("title"):
            job.duplicate_owned_title = str(matched["title"])
        if not job.duplicate_owned_track_id and matched.get("id"):
            job.duplicate_owned_track_id = str(matched["id"])
        return True

    def _destination_blocks_placement(
        self,
        db: Database,
        job: _ImportJob,
        desired_path: Path,
        metadata: dict[str, Any],
        matched_expected_track: Optional[dict[str, Any]],
    ) -> bool:
        """True when destination already imported in this pass or already exists and is not a strict upgrade."""
        if str(desired_path) in job.imported_paths:
            logger.warning("Two files map to %s; kept the first", desired_path)
            return True
        if desired_path.exists():
            existing = db.get_library_file_by_path(str(desired_path))
            is_upgrade = False
            if existing and matched_expected_track and str(existing.get("track_id")) == str(matched_expected_track.get("id")):
                is_upgrade = self._is_strict_upgrade(db, job, existing, metadata)
            if not is_upgrade:
                logger.warning("Two files map to %s; kept the first", desired_path)
                return True
        return False

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
                "title": _candidate_file_title(af, {}, job.item.get("artist")),
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
            metadata["title"] = _candidate_file_title(af, metadata, job.item.get("artist"))

        tag_artist = metadata.get("artist")
        tag_mbid = metadata.get("musicbrainz_artistid")

        # Reconcile against expected catalog tracks if present
        matched_expected_track = None
        file_weak_strength: Optional[str] = None
        if job.remaining_expected_tracks:
            matched_expected_track, match_strength = reconcile_audio_file_to_track_scored(
                metadata, job.remaining_expected_tracks
            )
            matched_expected_track, match_strength = self._prefer_wanted_track(
                job, metadata, matched_expected_track, match_strength
            )
            tag_track = matched_expected_track
            matched_expected_track = _fingerprint_fallback_match(
                af, ctx.media_settings, job.remaining_expected_tracks, matched_expected_track, match_strength
            )
            if matched_expected_track is not None and matched_expected_track is tag_track and match_strength != MATCH_STRONG:
                file_weak_strength = match_strength
            if matched_expected_track and self._existing_file_blocks_placement(
                db, job, af, metadata, matched_expected_track
            ):
                return
            if matched_expected_track:
                job.remaining_expected_tracks.remove(matched_expected_track)
                metadata["title"] = matched_expected_track["title"]
                metadata["track_number"] = int(matched_expected_track.get("track_number") or 1)
                metadata["disc_number"] = int(matched_expected_track.get("disc_number") or 1)
                matched_album = (
                    db.get_library_album(str(matched_expected_track["album_id"]))
                    if matched_expected_track.get("album_id")
                    and (not job.target_album or str(matched_expected_track["album_id"]) != str(job.target_album.get("id")))
                    else job.target_album
                )
                if matched_album:
                    _apply_catalog_album(
                        metadata,
                        matched_album,
                        db.get_library_artist(matched_album["artist_id"]) if matched_album.get("artist_id") else None,
                        ctx.media_settings,
                        db=db,
                    )

        if job.expected_tracks and matched_expected_track is None:
            cand_target = Path(build_track_path(metadata, ctx.media_settings)).resolve()
            if str(cand_target) in job.imported_paths:
                logger.warning("Two files map to %s; kept the first", cand_target)
                return
            artist_id = _download_artist_id(db, job.item, job.target_album)
            title = metadata.get("title") or ""
            duration = metadata.get("duration") or metadata.get("duration_seconds")
            owned = find_owned_for_item(
                db,
                job.item,
                title,
                duration,
                tag_artist=metadata.get("artist"),
                artist_id=artist_id,
            )
            if owned and owned.get("cutoff_met"):
                logger.info(
                    "Download %s: %s is already in the library as %s; discarding duplicate",
                    job.download_id,
                    title,
                    owned.get("title"),
                )
                job.duplicate_files.append(str(af))
                if not job.duplicate_owned_title and owned.get("title"):
                    job.duplicate_owned_title = str(owned["title"])
                if not job.duplicate_owned_track_id and owned.get("id"):
                    job.duplicate_owned_track_id = str(owned["id"])
                return

            if job.selected_track_match:
                sel_path, sel_track = job.selected_track_match
                if af.resolve() == sel_path.resolve():
                    matched_expected_track = sel_track
                    if self._existing_file_blocks_placement(
                        db, job, af, metadata, matched_expected_track
                    ):
                        return
                    if matched_expected_track in job.remaining_expected_tracks:
                        job.remaining_expected_tracks.remove(matched_expected_track)
                    metadata["title"] = matched_expected_track["title"]
                    metadata["track_number"] = int(matched_expected_track.get("track_number") or 1)
                    metadata["disc_number"] = int(matched_expected_track.get("disc_number") or 1)
                    matched_album = (
                        db.get_library_album(str(matched_expected_track["album_id"]))
                        if matched_expected_track.get("album_id")
                        and (not job.target_album or str(matched_expected_track["album_id"]) != str(job.target_album.get("id")))
                        else job.target_album
                    )
                    if matched_album:
                        _apply_catalog_album(
                            metadata,
                            matched_album,
                            db.get_library_artist(matched_album["artist_id"]) if matched_album.get("artist_id") else None,
                            ctx.media_settings,
                            db=db,
                        )

            if matched_expected_track is None:
                matched_expected_track = self._cross_album_match(db, ctx, job, metadata, duration)
                if matched_expected_track is None:
                    logger.warning(
                        "Holding unmatched file %s for download %s (manual import required)", af, job.download_id
                    )
                    job.held_files.append(str(af))
                    return

        # Disc 1 of a multi-disc release must use the multi-disc format only when owned files span multiple discs.
        known_discs: list[int] = [1]
        try:
            known_discs.append(int(metadata.get("disc_number") or 1))
        except (TypeError, ValueError):
            pass
        for t in job.placed_to_track.values():
            try:
                known_discs.append(int(t.get("disc_number") or 1))
            except (TypeError, ValueError):
                pass
        target_album_for_file = (
            db.get_library_album(str(matched_expected_track["album_id"]))
            if matched_expected_track and matched_expected_track.get("album_id")
            else job.target_album
        )
        if target_album_for_file and target_album_for_file.get("id"):
            known_discs.append(db.max_owned_disc(str(target_album_for_file["id"])))
        metadata["total_discs"] = max(known_discs)


        if not metadata.get("artist_folder"):
            existing_f = _existing_folder_for_tags(db, metadata, ctx.media_settings)
            if existing_f:
                metadata["artist_folder"] = existing_f

        metadata["track_credits"] = naming_credits(
            db,
            matched_expected_track.get("id") if matched_expected_track else None,
            str(metadata.get("title") or ""),
            tag_artist,
            tag_mbid,
        )
        job.last_metadata = metadata

        target_str = build_track_path(metadata, ctx.media_settings)
        desired_path = Path(target_str).resolve()
        if self._destination_blocks_placement(db, job, desired_path, metadata, matched_expected_track):
            return

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

        if str(target_path) != str(desired_path) and self._destination_blocks_placement(
            db, job, target_path, metadata, matched_expected_track
        ):
            if pre_recycled is not None:
                restore_recycled(pre_recycled[0])
            return

        if target_path.exists() and pre_recycled is None:
            logger.warning("Two files map to %s; kept the first", target_path)
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
        track_artist: Optional[str] = None
        track_credits = metadata.get("track_credits")
        if isinstance(track_credits, list) and track_credits:
            track_title = str(metadata.get("title") or (job.req.get("title") if job.req else "") or "")
            credited_artist, _ = tag_artist_from_credits(
                track_credits,
                track_title,
                ctx.media_settings,
            )
            if credited_artist is not None:
                track_artist = credited_artist

        tags_to_write: dict[str, Any] = build_tags_to_write(
            metadata,
            req=job.req,
            audio_files_count=len(job.audio_files),
            artist=track_artist,
            album_artist=metadata.get("album_artist") or None,
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
            if not job.held_msg:
                job.held_msg = f"{len(job.held_files)} file(s) couldn't be matched — manual import required"
            if str(job.item.get("error_message") or "").startswith("Retried import"):
                job.held_msg = f"Retried import: {job.held_msg}"
            if not job.imported_paths:
                # Nothing placed: park the download for manual import (not a failure, no blocklisting).
                db.update_download_status(
                    job.download_id, status=DownloadStatus.WARNING.value, error_message=job.held_msg
                )
                return False

        if not job.imported_paths and not job.held_files and job.duplicate_files:
            self._settle_duplicate(db, ctx, job)
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

    def _owned_alias_for_wanted(
        self,
        db: Database,
        job: _ImportJob,
        title: str,
        dur: Optional[float],
    ) -> Optional[dict[str, Any]]:
        """Finds an owned duplicate track alias for a wanted song when titles match and durations don't conflict."""
        track_id = job.duplicate_owned_track_id
        if not track_id:
            return None
        try:
            trk = db.get_library_track(track_id)
            f = db.get_library_file_for_track(track_id)
        except sqlite3.Error as exc:
            logger.warning("Failed to lookup duplicate owned track %s: %s", track_id, exc)
            return None
        if not trk or not f:
            return None
        if not same_song_title(trk.get("title"), title):
            return None
        trk_dur = trk.get("duration_seconds")
        try:
            trk_dur_val = float(trk_dur) if trk_dur is not None else None
        except (ValueError, TypeError):
            trk_dur_val = None
        try:
            dur_val = float(dur) if dur is not None else None
        except (ValueError, TypeError):
            dur_val = None
        if (
            dur_val is not None
            and dur_val > 0
            and trk_dur_val is not None
            and trk_dur_val > 0
            and abs(dur_val - trk_dur_val) > 5.0
        ):
            return None
        logger.info(
            "Download %s: wanted '%s' is owned as '%s'",
            job.download_id,
            title,
            trk.get("title"),
        )
        return {**trk, "cutoff_met": bool(f.get("cutoff_met"))}

    def _settle_duplicate(
        self,
        db: Database,
        ctx: _PollContext,
        job: _ImportJob,
    ) -> None:
        """Clears a completed download whose files are already owned in the library."""
        if job.driver_type == "slskd" or job.import_mode == "move":
            for dup in job.duplicate_files:
                try:
                    Path(dup).unlink(missing_ok=True)
                except OSError as e:
                    logger.warning("Failed to delete duplicate file %s: %s", dup, e)
            self._prune_source_dirs(job, job.duplicate_files)

        active_dl = db.get_active_download(job.download_id) or job.item
        settle_transfer_after_import(
            job.driver,
            job.target_lookup,
            ctx.media_settings,
            job.import_mode,
            job.status_dict,
            active_dl,
            db,
        )

        artist_id = _download_artist_id(db, job.item, job.target_album)
        title, dur = wanted_song(db, job.item)
        track_id = job.item.get("track_id")
        has_file = bool(track_id and db.get_library_file_for_track(str(track_id)) is not None)
        owned = (
            find_owned_for_item(
                db,
                job.item,
                title,
                dur,
                exclude_track_id=track_id,
                artist_id=artist_id,
            )
            if title
            else None
        )
        if title and not has_file and not (owned and owned.get("cutoff_met")):
            alias_owned = self._owned_alias_for_wanted(db, job, title, dur)
            if alias_owned:
                owned = alias_owned
        satisfied = has_file or bool(owned and owned.get("cutoff_met"))

        status_msg = (
            "Already in library — duplicate discarded"
            if satisfied
            else "Discarded a file that is a different song you already own"
        )
        db.update_download_status(
            job.download_id,
            status=DownloadStatus.IMPORTED.value,
            error_message=status_msg,
        )

        if satisfied and job.item.get("request_id"):
            db.update_request_status(job.item["request_id"], RequestStatus.AVAILABLE.value)

        owned_elsewhere = bool(track_id and not has_file and owned and owned.get("cutoff_met"))
        if not satisfied or owned_elsewhere:
            owned_title = getattr(job, "duplicate_owned_title", None)
            if not owned_title and owned and owned.get("title"):
                owned_title = owned["title"]
            if not owned_title:
                owned_title = "another track"
            try:
                db.add_to_blocklist(
                    source_title=job.item["title"],
                    artist=job.item.get("artist"),
                    release_guid=(job.item.get("download_hash") or job.download_id),
                    info_hash=job.item.get("download_hash"),
                    reason=(
                        f"Already own this song as {owned_title}"
                        if satisfied
                        else f"Already own this file's song as {owned_title}"
                    ),
                    download_id=job.download_id,
                    track_id=job.item.get("track_id"),
                )
            except sqlite3.Error as exc:
                logger.warning("Failed to blocklist duplicate download %s: %s", job.download_id, exc)

        download_id = str(job.download_id or "") or None
        provenance = download_trigger_kwargs(db, download_id)
        common: dict[str, Any] = {
            "track_id": str(track_id) if track_id else None,
            "request_id": job.item.get("request_id"),
            "download_id": download_id,
            **provenance,
        }
        emit(
            db,
            "imported",
            message=status_msg,
            details={
                "duplicate_files": job.duplicate_files,
                "release": job.item.get("title"),
            },
            **common,
        )

        if satisfied and track_id and db.get_library_file_for_track(str(track_id)) is None:
            try:
                db.set_track_monitored(str(track_id), False)
                owned_title = owned.get("title") if owned else "equivalent track"
                logger.info("Unmonitored %s: already owned as %s", track_id, owned_title)
            except sqlite3.Error as exc:
                logger.warning("Failed to unmonitor duplicate track %s: %s", track_id, exc)

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

    def _prune_source_dirs(self, job: _ImportJob, files: Optional[list[str]] = None) -> int:
        """Removes empty folders left in the client's download root after files were moved or deleted."""
        roots = getattr(self, "allowed_roots", None)
        if job.held_files or roots is None:
            return 0
        grouped: dict[Path, list[str]] = {}
        for raw in files if files is not None else job.audio_files:
            try:
                root = roots.matching_root(Path(raw).resolve())
            except (OSError, RuntimeError, ValueError) as exc:
                logger.debug("Could not resolve %s for source pruning: %s", raw, exc)
                continue
            if root is not None:
                grouped.setdefault(root, []).append(str(raw))
        return sum(prune_empty_parents(paths, root) for root, paths in grouped.items())

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
            if job.import_mode == "move":
                self._prune_source_dirs(job)

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
            request_library_refresh(
                as_media_server(ctx.plex_client),
                reason="import",
                paths=job.imported_paths,
                db=db,
            )



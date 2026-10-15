"""Native artist ingestion and catalog building service."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import threading
import time
from typing import Any
import uuid

from trackseerr import art_pipeline
from trackseerr.api.routes.library._shared import validate_media_path
from trackseerr.artist_refresh import _store_total_tracks
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_monitoring import (
    DEFAULT_MONITOR_OPTION,
    album_monitored_for_option,
    hydrated_track_monitored,
    section_to_album_type,
)
from trackseerr.models import (
    LibraryAlbum,
    LibraryArtist,
    LibraryTrack,
)
from trackseerr.storage import Database, clean_library_name
from trackseerr.track_counts import positive_int as _positive_int

logger = logging.getLogger(__name__)


class MetadataProfileNotFound(ValueError):
    """Raised when an explicitly requested metadata profile cannot be found."""


def _run_in_background(target: Callable[[], None], name: str) -> None:
    """Runs ``target`` on a daemon thread (a seam tests replace to drive the job deterministically)."""
    threading.Thread(target=target, daemon=True, name=name).start()


def _deferred_profile_recompute_job(
    db: Database,
    enricher: MbidEnricherClient,
    discovery_client: DiscoveryClient | None,
    artist_id: str,
    artist_name: str,
) -> None:
    """Background half of a profile-bearing ingest: resolve the MBID, then run the normal artist refresh.

    The refresh persists MusicBrainz secondary types and (via ``Database.finish_pending_profile_recompute``) runs the
    deferred recompute once. Failures are logged and leave ``pending_profile_recompute`` set for the next refresh.
    """
    from trackseerr.artist_refresh_worker import artist_refresh_worker

    try:
        artist = db.get_library_artist(artist_id)
        if artist is None:
            return
        if not artist.get("mbid"):
            resolved = enricher.lookup_artist_mbid(artist_name)
            if not resolved:
                logger.warning("No MusicBrainz match for %s; profile recompute stays deferred", artist_name)
                return
            db.set_library_artist_mbid(artist_id, str(resolved))
        artist_refresh_worker.refresh_once(
            db=db, discovery_client=discovery_client, enricher=enricher, artist_ids=[artist_id]
        )
    except Exception as exc:  # background thread: nothing above us can handle it, so log the cause and keep the flag
        logger.warning(
            "Deferred metadata-profile recompute failed for %s (%s): %s", artist_name, artist_id, exc, exc_info=True
        )


def _defer_profile_recompute_after_ingest(
    db: Database,
    enricher: MbidEnricherClient,
    discovery_client: DiscoveryClient | None,
    artist_id: str,
    artist_name: str,
) -> None:
    """Makes a metadata profile effective for a freshly ingested artist without blocking the request.

    Discovery albums carry no secondary types, so everything would pass a studio-only profile. Ingest always sets
    ``pending_profile_recompute`` and queues a background MusicBrainz refresh that persists the types and recomputes
    once; a manual album/track edit before it finishes clears the flag and wins.
    """
    db.set_pending_profile_recompute(artist_id, True)
    _run_in_background(
        lambda: _deferred_profile_recompute_job(db, enricher, discovery_client, artist_id, artist_name),
        "ProfileRecomputeAfterIngest",
    )


@dataclass
class _IngestRun:
    """State across artist ingest phases."""

    db: Database
    discovery_client: DiscoveryClient
    artist_id: str
    artist_folder: str
    artist_monitored: bool
    monitor_option: str
    artist_added_at: str
    metadata_profile: dict[str, Any] | None
    seen_album_ids: set[str] = field(default_factory=set)
    albums_ingested: int = 0
    tracks_ingested: int = 0


def _ingest_album_tracks(
    run: _IngestRun,
    album_id: str,
    foreign_album_id: str,
) -> None:
    """Fetches album details from discovery client and upserts library tracks."""
    album_details = None
    try:
        time.sleep(0.01)
        album_details = run.discovery_client.get_album_details(foreign_album_id)
    except Exception as exc:
        logger.warning(
            "Discovery client get_album_details failed for %s: %s",
            foreign_album_id,
            exc,
        )

    if album_details:
        _store_total_tracks(run.db, album_id, album_details.get("track_count"))
    if album_details and isinstance(album_details.get("tracks"), list):
        for trk in album_details["tracks"]:
            foreign_track_id = trk.get("id")
            existing_trk = None
            if foreign_track_id:
                existing_trk = run.db.get_library_track_by_foreign_id(
                    foreign_track_id, album_id=album_id
                )
            if not existing_trk:
                existing_trk = run.db.get_library_track_by_title(
                    album_id,
                    trk.get("title", ""),
                    track_number=trk.get("track_number"),
                    disc_number=trk.get("disc_number") or None,
                )
            track_id = existing_trk["id"] if existing_trk else str(uuid.uuid4())
            trk_title = trk.get("title") or "Unknown Track"
            trk_num = int(trk.get("track_number") or 1)
            disc_num = int(trk.get("disc_number") or 1)
            dur = (
                float(trk["duration_seconds"])
                if trk.get("duration_seconds") is not None
                else None
            )

            run.db.upsert_library_track(
                LibraryTrack(
                    id=track_id,
                    album_id=album_id,
                    artist_id=run.artist_id,
                    title=trk_title,
                    clean_title=clean_library_name(trk_title),
                    track_number=trk_num,
                    disc_number=disc_num,
                    duration_seconds=dur,
                    monitored=(
                        bool(existing_trk["monitored"])
                        if existing_trk and run.monitor_option == "existing"
                        else hydrated_track_monitored(run.monitor_option)
                    ),
                    foreign_track_id=foreign_track_id,
                )
            )
            run.tracks_ingested += 1


def _ingest_album(
    run: _IngestRun,
    section_name: str,
    album: dict[str, Any],
) -> None:
    """Ingests a single album from discovery metadata into the native catalog."""
    foreign_album_id = album.get("id")
    if foreign_album_id and foreign_album_id in run.seen_album_ids:
        return
    if foreign_album_id:
        run.seen_album_ids.add(foreign_album_id)

    alb_monitored = album_monitored_for_option(
        run.monitor_option,
        artist_monitored=run.artist_monitored,
        album_type=section_to_album_type(section_name),
        has_files=False,
        release_date=album.get("release_date"),
        year=album.get("year"),
        artist_added_at=run.artist_added_at,
        profile=run.metadata_profile,
    )
    album_title = album.get("title") or "Unknown Album"
    year_val: int | None = None
    if album.get("year") is not None:
        try:
            year_val = int(album["year"])
        except (ValueError, TypeError):
            pass
    if year_val is None and album.get("release_date"):
        rdate = str(album["release_date"]).strip()
        if len(rdate) >= 4 and rdate[:4].isdigit():
            year_val = int(rdate[:4])

    album_type = album.get("record_type") or (
        "single" if section_name == "singles_eps" else (
            "compilation" if section_name == "compilations" else "album"
        )
    )

    existing_alb = None
    if foreign_album_id:
        existing_alb = run.db.get_library_album_by_foreign_id(foreign_album_id)
    if not existing_alb:
        existing_alb = run.db.get_library_album_by_title(run.artist_id, album_title)

    album_id = existing_alb["id"] if existing_alb else str(uuid.uuid4())
    album_path = str(Path(run.artist_folder) / album_title)

    run.db.upsert_library_album(
        LibraryAlbum(
            id=album_id,
            artist_id=run.artist_id,
            title=album_title,
            clean_title=clean_library_name(album_title),
            foreign_album_id=foreign_album_id,
            release_date=album.get("release_date"),
            year=year_val,
            album_type=album_type,
            monitored=alb_monitored,
            path=album_path,
            cover_url=album.get("cover_url"),
            total_tracks=_positive_int(album.get("track_count")),
        )
    )
    run.albums_ingested += 1

    if alb_monitored and foreign_album_id:
        _ingest_album_tracks(run, album_id, foreign_album_id)


def ingest_artist_native(
    db: Database,
    discovery_client: DiscoveryClient,
    enricher: MbidEnricherClient,
    *,
    foreign_artist_id: str,
    artist_name: str,
    quality_profile_id: str | None = None,
    monitor_option: str | None = None,
    monitored: bool = True,
    root_folder: str | None = None,
    metadata_profile_id: int | None = None,
    metadata_profile_id_set: bool = False,
) -> dict[str, Any]:
    """Ingests an artist discography from discovery metadata into the native catalog."""
    existing_artist = None
    if foreign_artist_id:
        existing_artist = db.get_library_artist_by_foreign_id(foreign_artist_id)
    if not existing_artist and artist_name:
        existing_artist = db.get_library_artist_by_name(artist_name)

    if existing_artist:
        res = dict(existing_artist)
        res["albums_ingested"] = 0
        res["tracks_ingested"] = 0
        res["already_existed"] = True
        return res

    mm = db.get_media_management_settings()
    root_folder_str = root_folder or mm.get("root_folder_path") or "/music"
    if root_folder:
        validate_media_path(root_folder, db=db)
    root_path = Path(root_folder_str).resolve()
    artist_folder = str(root_path / artist_name)

    resolved_monitor_option = monitor_option or str(mm.get("add_monitor_option") or DEFAULT_MONITOR_OPTION)
    resolved_metadata_profile_id: int | None = (
        metadata_profile_id
        if metadata_profile_id_set
        else mm.get("add_metadata_profile_id")
    )
    metadata_profile: dict[str, Any] | None = None
    if resolved_metadata_profile_id is not None:
        metadata_profile = db.get_metadata_profile(resolved_metadata_profile_id)
        if metadata_profile is None:
            if metadata_profile_id_set:
                raise MetadataProfileNotFound(f"Metadata profile {resolved_metadata_profile_id} not found")
            resolved_metadata_profile_id = None
    artist_added_at = datetime.now(timezone.utc).date().isoformat()

    artist_id = str(uuid.uuid4())
    artist_dict = db.upsert_library_artist(
        LibraryArtist(
            id=artist_id,
            name=artist_name,
            clean_name=clean_library_name(artist_name),
            foreign_artist_id=foreign_artist_id,
            path=artist_folder,
            monitored=monitored,
            monitor_option=resolved_monitor_option,
            quality_profile_id=quality_profile_id,
            metadata_profile_id=resolved_metadata_profile_id,
        )
    )

    artist_details: dict[str, Any] | None = None
    try:
        artist_details = discovery_client.get_artist_details(foreign_artist_id)
    except Exception as exc:
        logger.warning(
            "Discovery client get_artist_details failed for %s: %s",
            foreign_artist_id,
            exc,
        )

    run = _IngestRun(
        db=db,
        discovery_client=discovery_client,
        artist_id=artist_id,
        artist_folder=artist_folder,
        artist_monitored=monitored,
        monitor_option=resolved_monitor_option,
        artist_added_at=artist_added_at,
        metadata_profile=metadata_profile,
    )

    if artist_details:
        sections = [
            ("albums", artist_details.get("albums") or []),
            ("singles_eps", artist_details.get("singles_eps") or []),
            ("compilations", artist_details.get("compilations") or []),
        ]

        for section_name, album_list in sections:
            for album in album_list:
                _ingest_album(run, section_name, album)

    if metadata_profile is not None and monitored and resolved_monitor_option not in ("existing", "none") and run.albums_ingested:
        _defer_profile_recompute_after_ingest(db, enricher, discovery_client, artist_id, artist_name)

    if resolved_monitor_option == "existing":
        # Track-level "existing" on add: monitor exactly the tracks that already have files (none for a new artist,
        # but a re-added folder may already own some).
        db.bulk_edit_library_artists([artist_id], apply_monitor_to_albums=True)

    # Pre-cache the artist image and every album cover (throttled, background) so the first view is local.
    art_pipeline.schedule_precache(db, artist_id)

    result = db.get_library_artist(artist_id) or artist_dict
    return {
        **result,
        "albums_ingested": run.albums_ingested,
        "tracks_ingested": run.tracks_ingested,
    }

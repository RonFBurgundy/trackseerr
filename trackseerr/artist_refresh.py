"""Service for background artist metadata refresh and filesystem reconciliation."""

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any, Optional
import uuid



from trackseerr import art_pipeline
from trackseerr.mb_metadata_store import get_shared_discovery_client, get_shared_enricher
from trackseerr.acquisition_worker import (
    reconcile_audio_file_to_track,
)
from trackseerr.clients.discovery import DiscoveryClient
from trackseerr.album_track_hydration import album_hydration_lock
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_monitoring import (
    album_monitored_for_option,
    normalize_secondary_types,
    hydrated_track_monitored,
    section_to_album_type,
)
from trackseerr.mediacover import mediacover_service
from trackseerr.models import (
    LibraryAlbum,
    LibraryFile,
    LibraryTrack,
)
from trackseerr.library import (
    AUDIO_EXTENSIONS,
    inspect_audio_file,
)
from trackseerr.system_paths import is_system_folder_name
from trackseerr.recycle_bin import (
    is_excluded_entry,
)
from trackseerr.storage import Database, clean_library_name
from trackseerr.track_counts import positive_int as _positive_int


logger = logging.getLogger(__name__)

def _rg_release_date(rg: dict[str, Any]) -> Optional[str]:
    """Release date of an enricher release group (``first_release_date``; legacy ``release_date`` accepted)."""
    return rg.get("first_release_date") or rg.get("release_date") or None

def _store_total_tracks(db: Database, album_id: str, count: Any, authoritative: bool = False) -> None:
    """Persists a provider-reported release track count on ``library_albums.total_tracks`` (ignored when unknown).

    Rule: the stored value only ever grows (``max(existing, new)``), because a provider may describe a shorter edition
    (Deezer's standard cut, a MusicBrainz first release without bonus discs) than the one already recorded. Pass
    ``authoritative=True`` only for a full MusicBrainz release with all media, which may replace the value outright.
    """
    n = _positive_int(count)
    if n is None:
        return
    db.set_library_album_total_tracks(album_id, n, authoritative=authoritative)

def _album_track_counts(db: Database, artist_id: str) -> tuple[dict[str, int], dict[str, int]]:
    """Per album of an artist: stored track rows, and rows that own at least one file."""
    with db._lock:
        rows = db.conn.execute(
            "SELECT t.album_id, COUNT(*), "
            "SUM(CASE WHEN EXISTS (SELECT 1 FROM library_files f WHERE f.track_id = t.id) THEN 1 ELSE 0 END) "
            "FROM library_tracks t WHERE t.artist_id = ? GROUP BY t.album_id",
            (artist_id,),
        ).fetchall()
    return {r[0]: int(r[1]) for r in rows}, {r[0]: int(r[2] or 0) for r in rows}

def _queue_release_date_update(
    existing_alb: dict[str, Any], rg: dict[str, Any], upd_album: list[str], upd_params: list[Any]
) -> None:
    """Queues a backfill of an existing album's missing release date / year from a MusicBrainz release group."""
    rdate = _rg_release_date(rg)
    if rdate and not existing_alb.get("release_date"):
        upd_album.append("release_date = ?")
        upd_params.append(str(rdate))
    if rg.get("year") is not None and existing_alb.get("year") is None:
        upd_album.append("year = ?")
        upd_params.append(rg["year"])
    if _positive_int(rg.get("track_count")) is not None and not existing_alb.get("total_tracks"):
        upd_album.append("total_tracks = ?")
        upd_params.append(_positive_int(rg.get("track_count")))

def reconcile_artist_files(db: Database, artist_id: str) -> int:
    """Matches unlinked or unassigned library_files in artist folder to canonical library_tracks."""
    artist = db.get_library_artist(artist_id)
    if not artist:
        return 0

    reconciled_count = 0
    candidate_tracks = db.list_library_tracks(artist_id=artist_id, limit=5000)
    if not candidate_tracks:
        return 0

    artist_path_str = artist.get("path")
    if not artist_path_str:
        return 0

    artist_path = Path(artist_path_str)
    if not artist_path.is_dir():
        return 0

    # 1. Look for orphaned library_files (files where track_id doesn't exist)
    with db._lock:
        cur = db.conn.execute(
            """
            SELECT f.* FROM library_files f
            LEFT JOIN library_tracks t ON f.track_id = t.id
            WHERE t.id IS NULL AND f.file_path LIKE ?
            """,
            (f"{artist_path_str}%",),
        )
        orphaned_files = [dict(r) for r in cur.fetchall()]

    for f in orphaned_files:
        fpath = Path(f["file_path"])
        if not fpath.is_file():
            continue
        try:
            meta = inspect_audio_file(fpath)
            matched = reconcile_audio_file_to_track(meta, candidate_tracks)
            if matched:
                with db._lock:
                    db.conn.execute(
                        "UPDATE library_files SET track_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (matched["id"], f["id"]),
                    )
                    db.conn.commit()
                reconciled_count += 1
        except Exception as exc:
            logger.debug("reconcile_artist_files: Failed matching orphaned file %s: %s", fpath, exc)

    # 2. Check tracks that have no library_file, and look for matching audio files on disk
    tracks_without_files = [
        t for t in candidate_tracks if db.get_library_file_for_track(t["id"]) is None
    ]
    if not tracks_without_files:
        return reconciled_count

    try:
        for audio_ext in AUDIO_EXTENSIONS:
            for disk_file in artist_path.rglob(f"*{audio_ext}"):
                if is_excluded_entry(disk_file, artist_path, []) or not disk_file.is_file():
                    continue
                existing_f = db.get_library_file_by_path(str(disk_file))
                if existing_f and db.get_library_track(existing_f["track_id"]):
                    continue

                try:
                    meta = inspect_audio_file(disk_file)
                    matched = reconcile_audio_file_to_track(meta, tracks_without_files)
                    if matched:
                        rel_path = (
                            str(disk_file.relative_to(artist_path.parent))
                            if artist_path.parent != artist_path
                            else disk_file.name
                        )
                        file_id = str(existing_f["id"]) if existing_f else str(uuid.uuid4())
                        file_size = 0
                        try:
                            file_size = disk_file.stat().st_size
                        except OSError:
                            pass

                        db.upsert_library_file(
                            LibraryFile(
                                id=file_id,
                                track_id=matched["id"],
                                file_path=str(disk_file),
                                relative_path=rel_path,
                                codec=str(meta.get("codec") or disk_file.suffix.lstrip(".").upper() or "UNKNOWN"),
                                bitrate=meta.get("bitrate"),
                                sample_rate=meta.get("sample_rate"),
                                bits_per_sample=meta.get("bits_per_sample"),
                                quality_name=str(meta.get("quality_full") or "Unknown"),
                                size_bytes=file_size,
                                cutoff_met=True,
                            )
                        )
                        tracks_without_files = [t for t in tracks_without_files if t["id"] != matched["id"]]
                        reconciled_count += 1
                except Exception as exc:
                    logger.debug("reconcile_artist_files: Failed inspecting disk file %s: %s", disk_file, exc)
    except Exception as exc:
        logger.debug("reconcile_artist_files: Error traversing artist directory %s: %s", artist_path, exc)

    return reconciled_count

def refresh_single_artist(
    artist_id: str,
    db: Database,
    discovery_client: Optional[DiscoveryClient] = None,
    enricher: Optional[MbidEnricherClient] = None,
    force: bool = False,
) -> dict[str, Any]:
    """Refreshes artist metadata, canonical discography, full tracklist hydration, artwork, and file reconciliation."""
    if enricher is None:
        enricher = get_shared_enricher(db)
    if discovery_client is None:
        discovery_client = get_shared_discovery_client(db)

    enricher_stats_before = enricher.stats()
    discovery_stats_before = discovery_client.stats()
    source_unavailable = False
    discography_fetched = False

    artist = db.get_library_artist(artist_id)
    if artist is None:
        return {"success": False, "message": "Artist not found", "artist_id": artist_id}

    artist_name = str(artist.get("name") or "").strip()
    if is_system_folder_name(artist_name):
        logger.info("Skipping metadata refresh for artist %r (%s): name is an OS/NAS system or trash folder", artist_name, artist_id)
        return {"success": False, "message": "Artist is a system folder; skipped", "artist_id": artist_id}
    foreign_artist_id = artist.get("foreign_artist_id")
    # Optional metadata profile: shapes only the monitored flag of albums created by this refresh.
    metadata_profile = db.get_metadata_profile(artist["metadata_profile_id"]) if artist.get("metadata_profile_id") else None

    # Check circuit breaker before MusicBrainz operations
    if not enricher.source_available():
        source_unavailable = True

    # 1. Enrich with MusicBrainz metadata and discography
    mbid = artist.get("mbid")
    if not source_unavailable and not mbid and not foreign_artist_id and artist_name:
        try:
            mbid = enricher.lookup_artist_mbid(artist_name)
            if mbid:
                with db._lock:
                    db.conn.execute(
                        "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                        (mbid, artist_id),
                    )
                    db.conn.commit()
                artist["mbid"] = mbid
        except Exception as exc:
            logger.warning("Error looking up artist MBID for %s: %s", artist_name, exc)

    mb_discography_found = False
    discography_complete = False
    fresh_rg_ids: set[str] = set()
    relinked_by_title_ids: set[str] = set()

    if not source_unavailable and mbid:
        try:
            mb_details = enricher.get_artist_details(mbid, force=force)
            if not enricher.source_available():
                source_unavailable = True
            if mb_details:
                new_artist_mbid = mb_details.get("id")
                if new_artist_mbid and str(new_artist_mbid).strip().lower() != str(mbid).strip().lower():
                    mbid = str(new_artist_mbid).strip()
                    artist["mbid"] = mbid
                    with db._lock:
                        db.conn.execute(
                            "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (mbid, artist_id),
                        )
                        db.conn.commit()

                country = mb_details.get("country")
                genres_raw = mb_details.get("genres")
                genres_str = (
                    ", ".join(genres_raw)
                    if isinstance(genres_raw, (list, tuple))
                    else (str(genres_raw) if genres_raw else None)
                )
                bio = mb_details.get("bio") or mb_details.get("disambiguation")

                art_updates: list[str] = []
                art_params: list[Any] = []
                if country and not artist.get("country"):
                    art_updates.append("country = ?")
                    art_params.append(country)
                    artist["country"] = country

                if genres_str and not artist.get("genres"):
                    art_updates.append("genres = ?")
                    art_params.append(genres_str)
                    artist["genres"] = genres_str

                if bio and not artist.get("bio"):
                    art_updates.append("bio = ?")
                    art_params.append(bio)
                    artist["bio"] = bio

                if art_updates:
                    art_updates.append("updated_at = CURRENT_TIMESTAMP")
                    sql = f"UPDATE library_artists SET {', '.join(art_updates)} WHERE id = ?"
                    art_params.append(artist_id)
                    with db._lock:
                        db.conn.execute(sql, art_params)
                        db.conn.commit()

            if not source_unavailable:
                discography, discography_complete = enricher.get_artist_discography_result(mbid, force=force)
                discography_fetched = True
                if not enricher.source_available():
                    source_unavailable = True
                if discography and len(discography) > 0:
                    mb_discography_found = True
                    artist_path = artist.get("path")
                    monitor_opt = artist.get("monitor_option", "all")
                    for rg in discography:
                        rg_id = rg.get("id")
                        if rg_id:
                            fresh_rg_ids.add(str(rg_id))
                        title = rg.get("title") or "Unknown Album"
                        album_type = rg.get("album_type", "album")

                        alb_monitored = album_monitored_for_option(
                            monitor_opt,
                            artist_monitored=bool(artist.get("monitored", True)),
                            album_type=album_type,
                            has_files=False,
                            release_date=_rg_release_date(rg),
                            year=rg.get("year"),
                            artist_added_at=artist.get("created_at"),
                            profile=metadata_profile,
                            secondary_types=rg.get("secondary_types"),
                        )

                        existing_alb = None
                        if rg_id:
                            existing_alb = db.get_library_album_by_release_group_id(rg_id)
                        if not existing_alb:
                            existing_alb = db.get_library_album_by_title(artist_id, title)
                            if existing_alb and existing_alb.get("mb_release_group_id"):
                                relinked_by_title_ids.add(str(existing_alb["mb_release_group_id"]))

                        if existing_alb:
                            album_id = existing_alb["id"]
                            alb_monitored = bool(existing_alb["monitored"])
                            upd_album: list[str] = []
                            upd_params: list[Any] = []
                            if rg_id and existing_alb.get("mb_release_group_id") != rg_id:
                                upd_album.append("mb_release_group_id = ?")
                                upd_params.append(rg_id)
                            if not existing_alb.get("cover_url") and rg.get("cover_url"):
                                upd_album.append("cover_url = ?")
                                upd_params.append(rg["cover_url"])
                            _queue_release_date_update(existing_alb, rg, upd_album, upd_params)
                            # Backfill/refresh the MusicBrainz secondary types; never touches the monitored flag.
                            if normalize_secondary_types(rg.get("secondary_types")) is not None and normalize_secondary_types(
                                rg["secondary_types"]
                            ) != existing_alb.get("secondary_types"):
                                upd_album.append("secondary_types = ?")
                                upd_params.append(json.dumps(normalize_secondary_types(rg["secondary_types"])))
                            if upd_album:
                                upd_album.append("updated_at = CURRENT_TIMESTAMP")
                                alb_sql = f"UPDATE library_albums SET {', '.join(upd_album)} WHERE id = ?"
                                upd_params.append(album_id)
                                with db._lock:
                                    db.conn.execute(alb_sql, upd_params)
                                    db.conn.commit()
                        else:
                            album_id = str(uuid.uuid4())
                            alb_path = str(Path(artist_path) / title) if artist_path else None
                            db.upsert_library_album(
                                LibraryAlbum(
                                    id=album_id,
                                    artist_id=artist_id,
                                    title=title,
                                    clean_title=clean_library_name(title),
                                    mb_release_group_id=rg_id,
                                    album_type=album_type,
                                    release_date=_rg_release_date(rg),
                                    year=rg.get("year"),
                                    cover_url=rg.get("cover_url"),
                                    secondary_types=rg.get("secondary_types"),
                                    monitored=alb_monitored,
                                    path=alb_path,
                                    total_tracks=_positive_int(rg.get("track_count")),
                                )
                            )

                        # Cache cover artwork via mediacover
                        cov_url = rg.get("cover_url") or (existing_alb.get("cover_url") if existing_alb else None)
                        if cov_url:
                            try:
                                mediacover_service.ensure_artwork("album_cover", album_id, cov_url)
                            except Exception as c_err:
                                logger.debug("Error caching cover for album %s: %s", album_id, c_err)

                        # Track hydration: If album is monitored, hydrate canonical tracks
                        if alb_monitored and rg_id:
                            tracks = None
                            if not source_unavailable:
                                tracks = enricher.get_release_group_tracks(rg_id, force=force)
                                if not enricher.source_available():
                                    source_unavailable = True
                            if not tracks and discovery_client:
                                # Fallback to Deezer album search/details for that album title
                                try:
                                    dz_results = discovery_client.search(f"{artist_name} {title}", item_type="album", limit=3)
                                    if isinstance(dz_results, list):
                                        for dz_item in dz_results:
                                            if clean_library_name(dz_item.get("title") or "") == clean_library_name(title):
                                                dz_alb_details = discovery_client.get_album_details(dz_item["id"], force=force)
                                                if dz_alb_details and dz_alb_details.get("tracks"):
                                                    tracks = [
                                                        {
                                                            "track_number": int(t.get("track_number") or 1),
                                                            "disc_number": int(t.get("disc_number") or 1),
                                                            "title": t.get("title") or "Unknown Track",
                                                            "duration_seconds": float(t["duration_seconds"]) if t.get("duration_seconds") is not None else None,
                                                            "mb_recording_id": None,
                                                        }
                                                        for t in dz_alb_details["tracks"]
                                                    ]
                                                    if dz_alb_details.get("cover_url") and not rg.get("cover_url"):
                                                        mediacover_service.ensure_artwork("album_cover", album_id, dz_alb_details["cover_url"])
                                                    break
                                except Exception as dz_err:
                                    logger.debug("Deezer track fallback failed for %s - %s: %s", artist_name, title, dz_err)

                            if tracks:
                                _store_total_tracks(db, album_id, len(tracks))
                                with album_hydration_lock(album_id):
                                    for trk in tracks:
                                        trk_title = trk.get("title") or "Unknown Track"
                                        trk_num = int(trk.get("track_number") or 1)
                                        disc_num = int(trk.get("disc_number") or 1)
                                        dur = trk.get("duration_seconds")
                                        mb_rec_id = trk.get("mb_recording_id")

                                        existing_trk = db.get_library_track_by_title(
                                            album_id,
                                            trk_title,
                                            track_number=trk_num,
                                        )
                                        if existing_trk:
                                            trk_id = existing_trk["id"]
                                            t_monitored = bool(existing_trk["monitored"])
                                        else:
                                            trk_id = str(uuid.uuid4())
                                            t_monitored = hydrated_track_monitored(monitor_opt)

                                        db.upsert_library_track(
                                            LibraryTrack(
                                                id=trk_id,
                                                album_id=album_id,
                                                artist_id=artist_id,
                                                title=trk_title,
                                                clean_title=clean_library_name(trk_title),
                                                track_number=trk_num,
                                                disc_number=disc_num,
                                                duration_seconds=dur,
                                                monitored=t_monitored,
                                                mb_recording_id=mb_rec_id,
                                            )
                                        )

            # Release-group redirect check (only when discography_complete is true)
            if discography_complete and not source_unavailable:
                try:
                    candidate_albums: list[dict[str, Any]] = []
                    offset = 0
                    while True:
                        page = db.list_library_albums(artist_id=artist_id, limit=500, offset=offset)
                        candidate_albums.extend(page)
                        if len(page) < 500:
                            break
                        offset += 500

                    rg_lookup_count = 0
                    for alb in candidate_albums:
                        if rg_lookup_count >= 50:
                            break
                        old_rg_id = alb.get("mb_release_group_id")
                        if not old_rg_id:
                            continue
                        str_old_rg = str(old_rg_id).strip()
                        if str_old_rg in fresh_rg_ids or str_old_rg in relinked_by_title_ids:
                            continue

                        rg_lookup_count += 1
                        new_rg_id = enricher.resolve_release_group(str_old_rg)
                        if not enricher.source_available():
                            source_unavailable = True
                            break
                        if new_rg_id:
                            clean_new_rg = str(new_rg_id).strip()
                            if clean_new_rg.lower() != str_old_rg.lower():
                                collision = False
                                for other_alb in candidate_albums:
                                    if other_alb["id"] != alb["id"] and other_alb.get("mb_release_group_id"):
                                        if str(other_alb["mb_release_group_id"]).strip().lower() == clean_new_rg.lower():
                                            collision = True
                                            break
                                if collision:
                                    logger.info(
                                        "Release group redirect %s -> %s for album %s skipped: target ID already on another album for artist %s",
                                        str_old_rg,
                                        clean_new_rg,
                                        alb["id"],
                                        artist_id,
                                    )
                                else:
                                    with db._lock:
                                        db.conn.execute(
                                            "UPDATE library_albums SET mb_release_group_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                            (clean_new_rg, alb["id"]),
                                        )
                                        db.conn.commit()
                                    alb["mb_release_group_id"] = clean_new_rg
                        else:
                            logger.info(
                                "Release group lookup for %s returned None; leaving album %s untouched",
                                str_old_rg,
                                alb["id"],
                            )
                except Exception as rg_exc:
                    logger.warning("Error during release group redirect check for artist %s: %s", artist_id, rg_exc)
        except Exception as exc:
            logger.warning("Error enriching artist %s via MusicBrainz: %s", artist_id, exc)
        else:
            db.finish_pending_profile_recompute(artist_id)

    # 2. Retrieve discography and artwork from Deezer only if MusicBrainz discography was not found
    if not mb_discography_found:
        if not foreign_artist_id and artist_name:
            try:
                d_art = discovery_client.search_artist(artist_name)
                if not isinstance(d_art, dict):
                    d_art = None
                if not d_art:
                    search_results = discovery_client.search(artist_name, item_type="all", limit=5)
                    if isinstance(search_results, list):
                        for item in search_results:
                            if isinstance(item, dict):
                                item_art = (item.get("artist") or "").lower().strip()
                                if item.get("item_type") == "artist" or item_art == artist_name.lower():
                                    item_id = item.get("id")
                                    if item_id and isinstance(item_id, (str, int)):
                                        d_art = {
                                            "id": str(item_id),
                                            "name": str(item.get("artist") or item.get("title") or ""),
                                            "image_url": str(item.get("cover_url")) if item.get("cover_url") else None,
                                        }
                                        break
                if isinstance(d_art, dict) and d_art.get("id") and isinstance(d_art["id"], (str, int)):
                    foreign_artist_id = str(d_art["id"])
                    artist["foreign_artist_id"] = foreign_artist_id
                    art_upd = ["foreign_artist_id = ?"]
                    art_params = [foreign_artist_id]
                    if d_art.get("image_url") and not artist.get("image_url") and isinstance(d_art["image_url"], str):
                        art_upd.append("image_url = ?")
                        art_params.append(d_art["image_url"])
                        artist["image_url"] = d_art["image_url"]
                    if d_art.get("banner_url") and not artist.get("banner_url") and isinstance(d_art["banner_url"], str):
                        art_upd.append("banner_url = ?")
                        art_params.append(d_art["banner_url"])
                        artist["banner_url"] = d_art["banner_url"]
                    art_upd.append("updated_at = CURRENT_TIMESTAMP")
                    art_params.append(artist_id)
                    with db._lock:
                        db.conn.execute(
                            f"UPDATE library_artists SET {', '.join(art_upd)} WHERE id = ?",
                            art_params,
                        )
                        db.conn.commit()
            except Exception as exc:
                logger.warning("Error resolving Deezer artist ID for '%s': %s", artist_name, exc)

        if foreign_artist_id:
            try:
                artist_details = discovery_client.get_artist_details(foreign_artist_id, force=force)
                if artist_details:
                    d_img = (
                        artist_details.get("image_url")
                        or artist_details.get("picture_xl")
                        or artist_details.get("picture_big")
                    )
                    d_banner = (
                        artist_details.get("banner_url")
                        or artist_details.get("picture_xl")
                    )
                    art_upd = []
                    art_params = []
                    if d_img and not artist.get("image_url"):
                        art_upd.append("image_url = ?")
                        art_params.append(d_img)
                        artist["image_url"] = d_img
                    if d_banner and not artist.get("banner_url"):
                        art_upd.append("banner_url = ?")
                        art_params.append(d_banner)
                        artist["banner_url"] = d_banner
                    if art_upd:
                        art_upd.append("updated_at = CURRENT_TIMESTAMP")
                        art_params.append(artist_id)
                        with db._lock:
                            db.conn.execute(
                                f"UPDATE library_artists SET {', '.join(art_upd)} WHERE id = ?",
                                art_params,
                            )
                            db.conn.commit()

                    sections = [
                        ("albums", artist_details.get("albums") or []),
                        ("singles_eps", artist_details.get("singles_eps") or []),
                        ("compilations", artist_details.get("compilations") or []),
                    ]
                    artist_path = artist.get("path")
                    seen_album_ids: set[str] = set()

                    for section_name, album_list in sections:
                        for album in album_list:
                            foreign_album_id = album.get("id")
                            if foreign_album_id and foreign_album_id in seen_album_ids:
                                continue
                            if foreign_album_id:
                                seen_album_ids.add(foreign_album_id)

                            album_title = album.get("title") or "Unknown Album"
                            existing_alb = None
                            if foreign_album_id:
                                existing_alb = db.get_library_album_by_foreign_id(foreign_album_id)
                            if not existing_alb:
                                existing_alb = db.get_library_album_by_title(artist_id, album_title)

                            year_val: Optional[int] = None
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

                            if existing_alb:
                                album_id = existing_alb["id"]
                                alb_monitored = bool(existing_alb["monitored"])
                                alb_path = existing_alb.get("path") or (
                                    str(Path(artist_path) / album_title) if artist_path else None
                                )
                                upd_cov = existing_alb.get("cover_url") or album.get("cover_url")
                                db.upsert_library_album(
                                    LibraryAlbum(
                                        id=album_id,
                                        artist_id=artist_id,
                                        title=album_title,
                                        clean_title=clean_library_name(album_title),
                                        foreign_album_id=foreign_album_id,
                                        release_date=album.get("release_date") or existing_alb.get("release_date"),
                                        year=year_val or existing_alb.get("year"),
                                        album_type=album_type,
                                        monitored=alb_monitored,
                                        path=alb_path,
                                        cover_url=upd_cov,
                                        mb_release_group_id=existing_alb.get("mb_release_group_id"),
                                        mb_release_id=existing_alb.get("mb_release_id"),
                                        total_tracks=_positive_int(album.get("track_count")),
                                    )
                                )
                            else:
                                album_id = str(uuid.uuid4())
                                monitor_opt = artist.get("monitor_option", "all")
                                alb_monitored = album_monitored_for_option(
                                    monitor_opt,
                                    artist_monitored=bool(artist.get("monitored", True)),
                                    album_type=section_to_album_type(section_name),
                                    has_files=False,
                                    release_date=album.get("release_date"),
                                    year=year_val,
                                    artist_added_at=artist.get("created_at"),
                                    profile=metadata_profile,
                                )

                                alb_path = str(Path(artist_path) / album_title) if artist_path else None
                                db.upsert_library_album(
                                    LibraryAlbum(
                                        id=album_id,
                                        artist_id=artist_id,
                                        title=album_title,
                                        clean_title=clean_library_name(album_title),
                                        foreign_album_id=foreign_album_id,
                                        release_date=album.get("release_date"),
                                        year=year_val,
                                        album_type=album_type,
                                        monitored=alb_monitored,
                                        path=alb_path,
                                        cover_url=album.get("cover_url"),
                                        total_tracks=_positive_int(album.get("track_count")),
                                    )
                                )

                            # Cache album cover
                            cov = album.get("cover_url") or (existing_alb.get("cover_url") if existing_alb else None)
                            if cov:
                                try:
                                    mediacover_service.ensure_artwork("album_cover", album_id, cov)
                                except Exception:
                                    pass

                            if alb_monitored and foreign_album_id:
                                album_details = None
                                try:
                                    album_details = discovery_client.get_album_details(foreign_album_id, force=force)
                                except Exception as exc:
                                    logger.warning(
                                        "Discovery client get_album_details failed during refresh for %s: %s",
                                        foreign_album_id,
                                        exc,
                                    )

                                if album_details:
                                    _store_total_tracks(db, album_id, album_details.get("track_count"))
                                if album_details and isinstance(album_details.get("tracks"), list):
                                    with album_hydration_lock(album_id):
                                        for trk in album_details["tracks"]:
                                            foreign_track_id = trk.get("id")
                                            existing_trk = None
                                            if foreign_track_id:
                                                existing_trk = db.get_library_track_by_foreign_id(
                                                    foreign_track_id, album_id=album_id
                                                )
                                            if not existing_trk:
                                                existing_trk = db.get_library_track_by_title(
                                                    album_id,
                                                    trk.get("title", ""),
                                                    track_number=trk.get("track_number"),
                                                )

                                            if existing_trk:
                                                track_id = existing_trk["id"]
                                                trk_monitored = bool(existing_trk["monitored"])
                                            else:
                                                track_id = str(uuid.uuid4())
                                                trk_monitored = hydrated_track_monitored(artist.get("monitor_option", "all"))

                                            trk_title = trk.get("title") or "Unknown Track"
                                            trk_num = int(trk.get("track_number") or 1)
                                            disc_num = int(trk.get("disc_number") or 1)
                                            dur = (
                                                float(trk["duration_seconds"])
                                                if trk.get("duration_seconds") is not None
                                                else None
                                            )

                                            db.upsert_library_track(
                                                LibraryTrack(
                                                    id=track_id,
                                                    album_id=album_id,
                                                    artist_id=artist_id,
                                                    title=trk_title,
                                                    clean_title=clean_library_name(trk_title),
                                                    track_number=trk_num,
                                                    disc_number=disc_num,
                                                    duration_seconds=dur,
                                                    monitored=trk_monitored,
                                                    foreign_track_id=foreign_track_id,
                                                )
                                            )
            except Exception as exc:
                logger.warning("Discovery client get_artist_details failed for refresh of %s: %s", foreign_artist_id, exc)

        # 3. Post-Deezer MusicBrainz metadata enrichment (bio, country, genres, and album release groups)
        if not source_unavailable and not mbid and artist_name:
            try:
                mbid = enricher.lookup_artist_mbid(artist_name)
                if not enricher.source_available():
                    source_unavailable = True
                if mbid:
                    with db._lock:
                        db.conn.execute(
                            "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                            (mbid, artist_id),
                        )
                        db.conn.commit()
                    artist["mbid"] = mbid
            except Exception as exc:
                logger.warning("Error looking up artist MBID for %s: %s", artist_name, exc)

        if not source_unavailable and mbid:
            try:
                mb_details = enricher.get_artist_details(mbid, force=force)
                if not enricher.source_available():
                    source_unavailable = True
                if mb_details:
                    new_artist_mbid = mb_details.get("id")
                    if new_artist_mbid and str(new_artist_mbid).strip().lower() != str(mbid).strip().lower():
                        mbid = str(new_artist_mbid).strip()
                        artist["mbid"] = mbid
                        with db._lock:
                            db.conn.execute(
                                "UPDATE library_artists SET mbid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                                (mbid, artist_id),
                            )
                            db.conn.commit()

                    country = mb_details.get("country")
                    genres_raw = mb_details.get("genres")
                    genres_str = (
                        ", ".join(genres_raw)
                        if isinstance(genres_raw, (list, tuple))
                        else (str(genres_raw) if genres_raw else None)
                    )
                    bio = mb_details.get("bio") or mb_details.get("disambiguation")

                    art_updates = []
                    art_params = []
                    if country and not artist.get("country"):
                        art_updates.append("country = ?")
                        art_params.append(country)
                        artist["country"] = country
                    if genres_str and not artist.get("genres"):
                        art_updates.append("genres = ?")
                        art_params.append(genres_str)
                        artist["genres"] = genres_str
                    if bio and not artist.get("bio"):
                        art_updates.append("bio = ?")
                        art_params.append(bio)
                        artist["bio"] = bio
                    if art_updates:
                        art_updates.append("updated_at = CURRENT_TIMESTAMP")
                        sql = f"UPDATE library_artists SET {', '.join(art_updates)} WHERE id = ?"
                        art_params.append(artist_id)
                        with db._lock:
                            db.conn.execute(sql, art_params)
                            db.conn.commit()

                if not source_unavailable:
                    discography, _ = enricher.get_artist_discography_result(
                        mbid, force=force and not discography_fetched
                    )
                    if not enricher.source_available():
                        source_unavailable = True
                    if discography:
                        for rg in discography:
                            rg_id = rg.get("id")
                            title = rg.get("title") or "Unknown Album"
                            existing_alb = None
                            if rg_id:
                                existing_alb = db.get_library_album_by_release_group_id(rg_id)
                            if not existing_alb:
                                existing_alb = db.get_library_album_by_title(artist_id, title)
                            if existing_alb:
                                upd_album = []
                                upd_params = []
                                if rg_id and existing_alb.get("mb_release_group_id") != rg_id:
                                    upd_album.append("mb_release_group_id = ?")
                                    upd_params.append(rg_id)
                                if not existing_alb.get("cover_url") and rg.get("cover_url"):
                                    upd_album.append("cover_url = ?")
                                    upd_params.append(rg["cover_url"])
                                _queue_release_date_update(existing_alb, rg, upd_album, upd_params)
                                if normalize_secondary_types(rg.get("secondary_types")) is not None and normalize_secondary_types(
                                    rg["secondary_types"]
                                ) != existing_alb.get("secondary_types"):
                                    upd_album.append("secondary_types = ?")
                                    upd_params.append(json.dumps(normalize_secondary_types(rg["secondary_types"])))
                                if upd_album:
                                    upd_album.append("updated_at = CURRENT_TIMESTAMP")
                                    alb_sql = f"UPDATE library_albums SET {', '.join(upd_album)} WHERE id = ?"
                                    upd_params.append(existing_alb["id"])
                                    with db._lock:
                                        db.conn.execute(alb_sql, upd_params)
                                        db.conn.commit()
            except Exception as exc:
                logger.warning("Error enriching artist %s via MusicBrainz: %s", artist_id, exc)
            else:
                db.finish_pending_profile_recompute(artist_id)

    # Cache artist poster & banner
    if artist.get("image_url"):
        try:
            mediacover_service.ensure_artwork("artist_poster", artist_id, artist["image_url"])
        except Exception:
            pass
    if artist.get("banner_url"):
        try:
            mediacover_service.ensure_artwork("artist_banner", artist_id, artist["banner_url"])
        except Exception:
            pass

    art_pipeline.schedule_precache(db, artist_id)

    # Reconcile files
    try:
        reconcile_artist_files(db, artist_id)
    except Exception as r_err:
        logger.warning("Error running file reconciliation for artist %s: %s", artist_id, r_err)

    enricher_stats_after = enricher.stats()
    discovery_stats_after = discovery_client.stats()

    net_reqs = enricher_stats_after.get("network_requests", 0) - enricher_stats_before.get("network_requests", 0)
    c_hits = enricher_stats_after.get("cache_hits", 0) - enricher_stats_before.get("cache_hits", 0)
    dz_reqs = discovery_stats_after.get("network_requests", 0) - discovery_stats_before.get("network_requests", 0)
    dz_hits = discovery_stats_after.get("cache_hits", 0) - discovery_stats_before.get("cache_hits", 0)

    if not foreign_artist_id and not mbid:
        res_dict = {
            "success": False,
            "message": "Artist has no linked discovery foreign ID or MusicBrainz ID",
            "network_requests": net_reqs,
            "cache_hits": c_hits,
            "deezer_requests": dz_reqs,
            "deezer_cache_hits": dz_hits,
        }
        if source_unavailable:
            res_dict["source_unavailable"] = True
        return res_dict

    res_dict = {
        "success": True,
        "artist_id": artist_id,
        "refreshed_at": datetime.now().isoformat(),
        "network_requests": net_reqs,
        "cache_hits": c_hits,
        "deezer_requests": dz_reqs,
        "deezer_cache_hits": dz_hits,
    }
    if source_unavailable:
        res_dict["source_unavailable"] = True
    return res_dict


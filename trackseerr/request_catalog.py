"""Catalog seeding for approved music requests in native library mode."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import requests

from trackseerr.clients.discovery import DiscoveryClient, DiscoveryUpstreamError
from trackseerr.clients.mbid_enricher import MbidEnricherClient
from trackseerr.library_manager import (
    MODE_NATIVE,
    get_library_mode,
    native_is_configured,
)
from trackseerr.mb_metadata_store import (
    get_shared_discovery_client,
    get_shared_enricher,
)
from trackseerr.models import LibraryTrack
from trackseerr.storage import Database, clean_library_name
from trackseerr.track_matching import find_owned_equivalent

logger = logging.getLogger(__name__)

DISCOVERY_ERRORS = (
    requests.RequestException,
    DiscoveryUpstreamError,
    KeyError,
    ValueError,
    TypeError,
)


def _resolve_foreign_artist(
    req: dict[str, Any],
    discovery_client: DiscoveryClient,
) -> tuple[str | None, str | None]:
    """Resolves foreign artist ID and canonical name using discovery client details or search."""
    foreign_id = req.get("foreign_id")
    artist_name = req.get("artist")

    if foreign_id:
        if foreign_id.startswith(("deezer:artist:", "itunes:artist:")):
            return foreign_id, artist_name

        item_type = req.get("item_type") or "track"
        if ":album:" in foreign_id or item_type == "album":
            details = discovery_client.get_album_details(foreign_id)
            if details:
                f_art_id = details.get("artist_id") or details.get("artist_discovery_id")
                name = details.get("artist") or artist_name
                if f_art_id:
                    return f_art_id, name

        if ":track:" in foreign_id or item_type == "track":
            details = discovery_client.get_track_details(foreign_id)
            if details:
                f_art_id = details.get("artist_discovery_id") or details.get("artist_id")
                name = details.get("artist") or artist_name
                if f_art_id:
                    return f_art_id, name

    if artist_name:
        art_info = discovery_client.search_artist(artist_name)
        if art_info and art_info.get("id"):
            return art_info["id"], art_info.get("name") or artist_name

    return None, None


def _ensure_artist(
    db: Database,
    req: dict[str, Any],
    discovery_client: DiscoveryClient,
    enricher: MbidEnricherClient,
) -> str | None:
    """Finds or ingests the library artist and ensures it is monitored without child cascade."""
    artist_name = req.get("artist")
    if artist_name:
        existing = db.get_library_artist_by_name(artist_name)
        if existing:
            artist_id = str(existing["id"])
            if not existing.get("monitored"):
                db.set_artist_monitored(artist_id, True, cascade_children=False)
            return artist_id

    foreign_artist_id, canonical_name = _resolve_foreign_artist(req, discovery_client)
    if not foreign_artist_id:
        return None

    resolved_name = canonical_name or artist_name or ""
    from trackseerr.artist_ingest import ingest_artist_native

    ingest_res = ingest_artist_native(
        db,
        discovery_client,
        enricher,
        foreign_artist_id=foreign_artist_id,
        artist_name=resolved_name,
        monitored=True,
        monitor_option="none",
    )
    artist_id = str(ingest_res["id"])
    if not ingest_res.get("monitored"):
        db.set_artist_monitored(artist_id, True, cascade_children=False)
    return artist_id


def _hydrate_album_tracks(
    db: Database,
    discovery_client: DiscoveryClient,
    artist_id: str,
    album_id: str,
    foreign_album_id: str | None,
) -> list[dict[str, Any]]:
    """Hydrates an album's tracks from discovery metadata if not yet present in catalog."""
    existing_tracks = db.list_library_tracks(album_id=album_id)
    if existing_tracks:
        return existing_tracks
    if not foreign_album_id:
        return []

    try:
        album_details = discovery_client.get_album_details(foreign_album_id)
    except DISCOVERY_ERRORS as exc:
        logger.warning("Failed to fetch album details for %s: %s", foreign_album_id, exc)
        return []

    if not album_details or not isinstance(album_details.get("tracks"), list):
        return []

    tracks_out = []
    for trk in album_details["tracks"]:
        f_trk_id = trk.get("id")
        existing_trk = None
        if f_trk_id:
            existing_trk = db.get_library_track_by_foreign_id(f_trk_id, album_id=album_id)
        if not existing_trk:
            existing_trk = db.get_library_track_by_title(
                album_id,
                trk.get("title", ""),
                track_number=trk.get("track_number"),
                disc_number=trk.get("disc_number") or None,
            )
        track_id = existing_trk["id"] if existing_trk else str(uuid.uuid4())
        trk_title = trk.get("title") or "Unknown Track"
        trk_num = int(trk.get("track_number") or 1)
        disc_num = int(trk.get("disc_number") or 1)
        dur = float(trk["duration_seconds"]) if trk.get("duration_seconds") is not None else None

        saved = db.upsert_library_track(
            LibraryTrack(
                id=track_id,
                album_id=album_id,
                artist_id=artist_id,
                title=trk_title,
                clean_title=clean_library_name(trk_title),
                track_number=trk_num,
                disc_number=disc_num,
                duration_seconds=dur,
                monitored=False,
                foreign_track_id=f_trk_id,
            )
        )
        tracks_out.append(saved)
    return tracks_out


def _seed_album_request(
    db: Database,
    req: dict[str, Any],
    artist_id: str,
    discovery_client: DiscoveryClient,
) -> tuple[str, str | None, str | None]:
    """Seeds an album request, monitoring the album and all its tracks."""
    album = None
    foreign_id = req.get("foreign_id")
    if foreign_id and (":album:" in foreign_id or not (":track:" in foreign_id or ":artist:" in foreign_id)):
        album = db.get_library_album_by_foreign_id(foreign_id)

    if not album:
        title = req.get("title") or req.get("album")
        if title:
            album = db.get_library_album_by_title(artist_id, title)

    if not album:
        return "unmatched", None, None

    album_id = str(album["id"])
    _hydrate_album_tracks(db, discovery_client, artist_id, album_id, album.get("foreign_album_id"))
    db.set_album_monitored(album_id, True, cascade_tracks=True)
    return "seeded", None, album_id


def _find_track_via_foreign_details(
    db: Database,
    artist_id: str,
    foreign_id: str,
    req_title: str,
    discovery_client: DiscoveryClient,
) -> dict[str, Any] | None:
    """Attempts to resolve a track by fetching foreign details, hydrating its album, and finding the track."""
    try:
        details = discovery_client.get_track_details(foreign_id)
        if not details:
            return None
        album_ref = details.get("album_discovery_id")
        album_title = details.get("album")
        trk_alb = None
        if album_ref:
            trk_alb = db.get_library_album_by_foreign_id(album_ref)
        if not trk_alb and album_title:
            trk_alb = db.get_library_album_by_title(artist_id, album_title)
        if trk_alb:
            _hydrate_album_tracks(
                db,
                discovery_client,
                artist_id,
                str(trk_alb["id"]),
                trk_alb.get("foreign_album_id") or album_ref,
            )
            trk = db.get_library_track_by_foreign_id(foreign_id)
            if not trk and req_title:
                trk = db.get_library_track_by_title(str(trk_alb["id"]), req_title)
            return trk
    except DISCOVERY_ERRORS as exc:
        logger.debug("Failed track details resolution for %s: %s", foreign_id, exc)
    return None


def _find_track(
    db: Database,
    req: dict[str, Any],
    artist_id: str,
    discovery_client: DiscoveryClient,
) -> dict[str, Any] | None:
    """Resolves track in native library catalog by album/title, foreign ID, or clean title."""
    album_name = req.get("album")
    alb = None
    foreign_id = req.get("foreign_id")
    if foreign_id and ":album:" in foreign_id:
        alb = db.get_library_album_by_foreign_id(foreign_id)
    if not alb and album_name:
        alb = db.get_library_album_by_title(artist_id, album_name)

    if alb:
        _hydrate_album_tracks(db, discovery_client, artist_id, str(alb["id"]), alb.get("foreign_album_id"))
        trk = db.get_library_track_by_title(str(alb["id"]), req.get("title", ""))
        if trk:
            return trk

    if foreign_id and ":album:" not in foreign_id and ":artist:" not in foreign_id:
        trk = db.get_library_track_by_foreign_id(foreign_id)
        if trk:
            return trk
        trk = _find_track_via_foreign_details(
            db, artist_id, foreign_id, req.get("title", ""), discovery_client
        )
        if trk:
            return trk

    title = req.get("title")
    if title:
        candidates = db.find_library_tracks_by_clean_title(title, artist_id=artist_id)
        if candidates:
            return candidates[0]

    return None


def _seed_track_request(
    db: Database,
    req: dict[str, Any],
    artist_id: str,
    discovery_client: DiscoveryClient,
) -> tuple[str, str | None, str | None]:
    """Seeds a track request, monitoring the track and its album without track cascade."""
    trk = _find_track(db, req, artist_id, discovery_client)
    if not trk:
        return "unmatched", None, None

    track_id = str(trk["id"])
    album_id = str(trk["album_id"]) if trk.get("album_id") else None

    if db.get_library_file_for_track(track_id) is None:
        owned = find_owned_equivalent(
            db,
            artist_id,
            req.get("title", ""),
            trk.get("duration_seconds"),
            exclude_track_id=track_id,
        )
        if owned:
            owned_id = str(owned["id"])
            owned_album_id = str(owned["album_id"]) if owned.get("album_id") else None
            return "seeded", owned_id, owned_album_id

    db.set_track_monitored(track_id, True)
    if album_id:
        db.set_album_monitored(album_id, True, cascade_tracks=False)

    return "seeded", track_id, album_id


def seed_request_catalog(
    db: Database,
    req: dict[str, Any],
    *,
    discovery_client: DiscoveryClient,
    enricher: MbidEnricherClient,
) -> str:
    """Seeds an approved request into the native catalog, monitoring only what was requested."""
    req_id = req.get("id")
    if not req_id:
        return "skipped"

    if get_library_mode(db) != MODE_NATIVE or not native_is_configured(db):
        db.set_request_catalog(req_id, status="skipped")
        logger.info(
            "Request %s (%s - %s) catalog seeding skipped: native mode not active",
            req_id,
            req.get("artist"),
            req.get("title"),
        )
        return "skipped"

    try:
        artist_id = _ensure_artist(db, req, discovery_client, enricher)
        if not artist_id:
            db.set_request_catalog(req_id, status="no_artist")
            logger.info(
                "Request %s (%s - %s) catalog seeding result: no_artist",
                req_id,
                req.get("artist"),
                req.get("title"),
            )
            return "no_artist"

        item_type = req.get("item_type") or "track"
        if item_type == "album":
            status, track_id, album_id = _seed_album_request(db, req, artist_id, discovery_client)
        else:
            status, track_id, album_id = _seed_track_request(db, req, artist_id, discovery_client)

        db.set_request_catalog(req_id, status=status, track_id=track_id, album_id=album_id)
        logger.info(
            "Request %s (%s - %s) catalog seeding result: %s (track=%s, album=%s)",
            req_id,
            req.get("artist"),
            req.get("title"),
            status,
            track_id,
            album_id,
        )
        return status
    except DISCOVERY_ERRORS:
        logger.exception(
            "Error seeding catalog for request %s (%s - %s)",
            req_id,
            req.get("artist"),
            req.get("title"),
        )
        db.set_request_catalog(req_id, status="error")
        return "error"


def seed_pending_request_catalog(
    db: Database,
    *,
    limit: int = 50,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, int]:
    """Sweeps unseeded approved requests and seeds them into the native catalog."""
    discovery_client = get_shared_discovery_client(db)
    enricher = get_shared_enricher(db)
    counts: dict[str, int] = {}

    pending = db.list_requests_needing_catalog(limit=limit)
    for req in pending:
        if should_stop and should_stop():
            break
        status = seed_request_catalog(
            db,
            req,
            discovery_client=discovery_client,
            enricher=enricher,
        )
        counts[status] = counts.get(status, 0) + 1

    return counts

"""Automated Search & Grab Coordinator for TrackSeerr Phase 3.

Coordinates multi-indexer searches across Torznab, Newznab, and slskd,
scores and ranks candidates against Quality Profiles, dispatches grabs to
appropriate download clients (qBittorrent, SABnzbd, slskd), and tracks active transfers.
"""

import logging
import re
import sqlite3
from typing import Any, Optional, Union
import uuid

from plex_playlist_sync.clients.acquisition import get_acquisition_driver, get_indexer_driver
from plex_playlist_sync.clients.acquisition.base import AcquisitionRetryableError, AcquisitionUnavailableError
from plex_playlist_sync.library_manager import MODE_NATIVE, ModeChanged, work_guard
from plex_playlist_sync.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadStatus,
    EvaluationResult,
    NotificationEvent,
    QualityProfile,
    QualityProfileItem,
)
from plex_playlist_sync.notifications import notification_dispatcher
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


_BTIH_RE = re.compile(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", re.IGNORECASE)


def _extract_info_hash(candidate: AcquisitionSearchResult) -> Optional[str]:
    """Extracts a torrent/p2p info hash from candidate URLs, extra payload, or download_id."""
    if candidate.magnet_url:
        m = _BTIH_RE.search(candidate.magnet_url)
        if m:
            return m.group(1).lower()
    if candidate.download_url:
        m = _BTIH_RE.search(candidate.download_url)
        if m:
            return m.group(1).lower()
    if candidate.extra and isinstance(candidate.extra, dict):
        h = candidate.extra.get("info_hash") or candidate.extra.get("hash")
        if h:
            return str(h).strip().lower()
    if candidate.download_id:
        dl_id = candidate.download_id.strip()
        if len(dl_id) in (32, 40) and re.fullmatch(r"[a-fA-F0-9]{32,40}", dl_id):
            return dl_id.lower()
    return None


def _to_quality_profile(data: Union[QualityProfile, dict[str, Any]]) -> QualityProfile:
    """Converts a dict or QualityProfile model to a canonical QualityProfile instance."""
    if isinstance(data, QualityProfile):
        return data

    items: list[QualityProfileItem] = []
    for item in data.get("items", []):
        if isinstance(item, QualityProfileItem):
            items.append(item)
        elif isinstance(item, dict):
            items.append(
                QualityProfileItem(
                    quality=str(item.get("quality", "Unknown")),
                    allowed=bool(item.get("allowed", True)),
                    weight=int(item.get("weight", 100)),
                )
            )

    return QualityProfile(
        id=str(data.get("id", "")),
        name=str(data.get("name", "")),
        cutoff=str(data.get("cutoff", "FLAC 16bit")),
        items=items,
        preferred_tags=list(data.get("preferred_tags") or []),
        ignored_tags=list(data.get("ignored_tags") or []),
        min_size_mb=(
            float(data["min_size_mb"]) if data.get("min_size_mb") is not None else None
        ),
        max_size_mb=(
            float(data["max_size_mb"]) if data.get("max_size_mb") is not None else None
        ),
        is_default=bool(data.get("is_default", False)),
        custom_formats=list(data.get("custom_formats") or []),
        min_score=(
            int(data["min_score"]) if data.get("min_score") is not None else None
        ),
    )


class AcquisitionCoordinator:
    """Coordinates search, quality evaluation, client dispatch, and active transfer records."""

    def search_all_indexers(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
        db: Optional[Database] = None,
    ) -> list[AcquisitionSearchResult]:
        """Queries all enabled Torznab/Newznab indexers and slskd clients.

        Errors on individual indexers or clients are logged without aborting others.
        """
        if db is None:
            logger.warning("No database supplied to search_all_indexers")
            return []

        all_results: list[AcquisitionSearchResult] = []

        # 1. Query enabled indexers (Torznab / Newznab)
        try:
            indexers = db.list_indexers(enabled_only=True)
        except Exception as e:
            logger.error("Failed to query enabled indexers from database: %s", e)
            indexers = []

        for idx_cfg in indexers:
            idx_name = idx_cfg.get("name", "unknown")
            try:
                driver = get_indexer_driver(idx_cfg)
                res = driver.search(artist=artist, title=title, album=album)
                if res:
                    all_results.extend(res)
            except Exception as e:
                logger.warning("Error searching indexer '%s' (%s): %s", idx_name, idx_cfg.get("host_url"), e)

        # 2. Query enabled slskd download clients if present
        try:
            download_clients = db.list_download_clients(enabled_only=True)
        except Exception as e:
            logger.error("Failed to query download clients from database: %s", e)
            download_clients = []

        slskd_clients = [
            c for c in download_clients
            if str(c.get("driver_type", "")).lower() == "slskd"
        ]
        for s_cfg in slskd_clients:
            s_name = s_cfg.get("name", "slskd")
            try:
                driver = get_acquisition_driver(s_cfg)
                res = driver.search(artist=artist, title=title, album=album)
                if res:
                    all_results.extend(res)
            except Exception as e:
                logger.warning("Error searching slskd client '%s' (%s): %s", s_name, s_cfg.get("host_url"), e)

        if db is not None:
            filtered_results: list[AcquisitionSearchResult] = []
            for candidate in all_results:
                btih = _extract_info_hash(candidate)
                if db.is_blocklisted(
                    release_title=candidate.title,
                    release_guid=candidate.download_id,
                    info_hash=btih,
                ):
                    logger.info("Skipping blocklisted release candidate during search: '%s'", candidate.title)
                    continue
                filtered_results.append(candidate)
            return filtered_results

        return all_results

    def evaluate_and_rank(
        self,
        candidates: list[AcquisitionSearchResult],
        profile: Union[QualityProfile, dict[str, Any]],
        db: Optional[Database] = None,
    ) -> list[tuple[AcquisitionSearchResult, EvaluationResult]]:
        """Evaluates candidates against QualityProfile and sorts by score descending.

        Ties for torrent releases are broken using seeder counts.
        """
        if not candidates:
            return []

        prof = _to_quality_profile(profile)
        ranked: list[tuple[AcquisitionSearchResult, EvaluationResult]] = []

        for candidate in candidates:
            if db is not None:
                btih = _extract_info_hash(candidate)
                if db.is_blocklisted(
                    release_title=candidate.title,
                    release_guid=candidate.download_id,
                    info_hash=btih,
                ):
                    logger.info("Skipping blocklisted release candidate during evaluation: '%s'", candidate.title)
                    continue

            parsed = parse_release_title(candidate.title)
            eval_res = evaluate_release(
                release=parsed,
                profile=prof,
                size_bytes=candidate.size_bytes if candidate.size_bytes > 0 else None,
            )
            if eval_res.is_acceptable:
                ranked.append((candidate, eval_res))

        def sort_key(item: tuple[AcquisitionSearchResult, EvaluationResult]) -> tuple[int, int]:
            cand, res = item
            is_torrent = cand.protocol == "torrent" or cand.source in ("torznab", "torrent") or bool(cand.magnet_url)
            seeders = int(cand.seeders or 0) if is_torrent else 0
            return (res.score, seeders)

        ranked.sort(key=sort_key, reverse=True)
        return ranked

    def find_client_for_protocol(
        self,
        protocol: str,
        db: Database,
    ) -> Optional[dict[str, Any]]:
        """Finds the enabled download client with the highest priority for the requested protocol.

        - "torrent": driver_type == "qbittorrent"
        - "usenet": driver_type == "sabnzbd"
        - "slskd": driver_type == "slskd"
        """
        proto = str(protocol).lower().strip()
        target_driver: Optional[str] = None

        if proto in ("torrent", "torznab"):
            target_driver = "qbittorrent"
        elif proto in ("usenet", "newznab"):
            target_driver = "sabnzbd"
        elif proto in ("slskd", "soulseek", "p2p"):
            target_driver = "slskd"

        if not target_driver:
            return None

        try:
            enabled_clients = db.list_download_clients(enabled_only=True)
        except Exception as e:
            logger.error("Failed to list download clients from database: %s", e)
            return None

        matching = [
            c for c in enabled_clients
            if str(c.get("driver_type", "")).lower() == target_driver
        ]
        if not matching:
            return None

        # Sort by priority ascending (1 = highest priority), then created_at
        matching.sort(key=lambda c: (int(c.get("priority", 1)), str(c.get("created_at", ""))))
        return matching[0]

    def search_and_grab(
        self,
        artist: str,
        title: str,
        album: Optional[str] = None,
        item_type: str = "track",
        request_id: Optional[str] = None,
        db: Optional[Database] = None,
        quality_profile_id: Optional[str] = None,
        min_score: Optional[int] = None,
        track_id: Optional[str] = None,
        album_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Searches indexers, ranks releases against the Quality Profile, and dispatches grab.

        Records active download transfer in the database upon successful dispatch. Runs under the native
        library-manager guard: if Lidarr manages the library it does nothing and reports ``mode_changed``.
        """
        if db is None:
            raise ValueError("Database instance must be provided to search_and_grab")
        try:
            with work_guard(db, MODE_NATIVE):
                return self._search_and_grab(
                    artist, title, album, item_type, request_id, db, quality_profile_id, min_score, track_id, album_id
                )
        except ModeChanged:
            logger.info("Native grab skipped for '%s - %s': library manager is Lidarr", artist, title)
            return {
                "success": False,
                "mode_changed": True,
                "message": "Library manager is set to Lidarr; native grabs are disabled.",
            }

    def _search_and_grab(
        self,
        artist: str,
        title: str,
        album: Optional[str],
        item_type: str,
        request_id: Optional[str],
        db: Database,
        quality_profile_id: Optional[str],
        min_score: Optional[int],
        track_id: Optional[str],
        album_id: Optional[str],
    ) -> dict[str, Any]:

        # 1. Retrieve quality profile
        profile_dict: Optional[dict[str, Any]] = None
        if quality_profile_id:
            try:
                profile_dict = db.get_quality_profile(quality_profile_id)
            except Exception as e:
                logger.warning("Error fetching quality profile %s: %s; falling back to default", quality_profile_id, e)

        if not profile_dict:
            try:
                profile_dict = db.get_default_quality_profile()
            except Exception as e:
                logger.error("Failed to retrieve default quality profile: %s", e)
                return {
                    "success": False,
                    "message": f"Quality profile not available: {str(e)}",
                }

        profile = _to_quality_profile(profile_dict)

        # 2. Search indexers & slskd
        candidates = self.search_all_indexers(artist=artist, title=title, album=album, db=db)

        # 3. Evaluate and rank
        ranked = self.evaluate_and_rank(candidates=candidates, profile=profile, db=db)
        if min_score is not None:
            ranked = [item for item in ranked if item[1].score > min_score]
        if not ranked:
            return {
                "success": False,
                "message": (
                    "No acceptable releases found meeting quality profile criteria"
                    if min_score is None
                    else f"No candidate release score exceeds current score {min_score}"
                ),
                "candidates_count": len(candidates),
            }

        top_candidate, eval_res = ranked[0]

        # 4. Find appropriate client for candidate's protocol
        client = self.find_client_for_protocol(protocol=top_candidate.protocol, db=db)
        if not client:
            return {
                "success": False,
                "message": f"No enabled download client available for {top_candidate.protocol}",
            }

        # 5. Dispatch download to client driver
        try:
            client_driver = get_acquisition_driver(client)
            download_hash = client_driver.download(top_candidate)
        except (AcquisitionRetryableError, AcquisitionUnavailableError) as e:
            retryable = isinstance(e, AcquisitionRetryableError)
            logger.warning(
                "Dispatch on client '%s' for '%s' %s: %s",
                client.get("name"),
                top_candidate.title,
                "needs a retry" if retryable else "is unavailable",
                e,
            )
            if request_id:
                try:  # let the requester see why it is stuck; the request status is unchanged
                    db.set_request_outcome(request_id, e.reason, str(e) or None)
                except sqlite3.Error as exc:
                    logger.warning("Could not record the dispatch outcome for request %s: %s", request_id, exc)
            return {
                "success": False,
                "retryable": retryable,
                "reason": e.reason,
                "message": f"Download dispatch failed: {e}",
            }
        except Exception as e:
            logger.error(
                "Dispatch download failed on client '%s' for '%s': %s",
                client.get("name"),
                top_candidate.title,
                e,
            )
            return {
                "success": False,
                "message": f"Download dispatch failed: {str(e)}",
            }

        # 6. Record active download in database
        download_id = f"dl-{uuid.uuid4().hex[:12]}"
        active_dl = ActiveDownload(
            id=download_id,
            request_id=request_id,
            client_id=str(client["id"]),
            download_hash=download_hash,
            title=top_candidate.title,
            artist=artist,
            item_type=item_type,
            status=DownloadStatus.QUEUED.value,
            progress=0.0,
            size_bytes=top_candidate.size_bytes,
            source_path=None,
            target_path=None,
            track_id=track_id,
            album_id=album_id,
        )
        db.create_active_download(active_dl)
        try:
            db.record_download_grab(
                download_id,
                indexer=str((top_candidate.extra or {}).get("indexer_name") or top_candidate.source or "") or None,
                quality=eval_res.parsed_quality,
                protocol=top_candidate.protocol or None,
                upgrade=min_score is not None,
            )
        except sqlite3.Error as hist_err:
            logger.warning("Failed to record grab history for %s: %s", download_id, type(hist_err).__name__)

        try:
            notification_dispatcher.dispatch(
                NotificationEvent.DOWNLOAD_STARTED,
                data={
                    "artist": artist,
                    "title": top_candidate.title,
                    "album": album,
                    "item_type": item_type,
                    "request_id": request_id,
                    "client": client.get("name"),
                    "release": top_candidate.title,
                    "download_id": download_id,
                    "size_bytes": top_candidate.size_bytes,
                },
                db=db,
            )
        except Exception as e:
            logger.warning("Failed to dispatch DOWNLOAD_STARTED notification: %s", e)

        try:
            client_name = client.get("name", "Unknown Client")
            db.record_event(
                "download_started",
                f"Grabbed '{top_candidate.title}' via {client_name}",
                source="AcquisitionWorker",
                severity="info",
                details={
                    "artist": artist,
                    "title": top_candidate.title,
                    "album": album,
                    "item_type": item_type,
                    "request_id": request_id,
                    "client": client_name,
                    "release": top_candidate.title,
                    "download_id": download_id,
                    "size_bytes": top_candidate.size_bytes,
                },
            )
        except Exception as ev_err:
            logger.warning("Failed to record download_started event: %s", ev_err)

        logger.info(
            "Successfully grabbed release '%s' via %s (download_id=%s, score=%d)",
            top_candidate.title,
            client.get("name"),
            download_id,
            eval_res.score,
        )

        return {
            "success": True,
            "download_id": download_id,
            "download_hash": download_hash,
            "release": top_candidate.title,
            "client": client["name"],
            "score": eval_res.score,
        }


# Module singleton
acquisition_coordinator = AcquisitionCoordinator()

__all__ = ["AcquisitionCoordinator", "acquisition_coordinator", "_to_quality_profile"]

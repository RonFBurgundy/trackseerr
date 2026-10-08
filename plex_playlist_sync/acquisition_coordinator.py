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
from plex_playlist_sync.item_history import GrabTrigger, emit, emit_named, trigger_kwargs
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
from plex_playlist_sync.decision_engine import (
    DurationInfo,
    candidate_context,
    evaluate_prepared,
    prepare_profile,
    rank_key,
    resolve_durations,
)
from plex_playlist_sync import delay_gate
from plex_playlist_sync.quality import evaluate_release, parse_release_title
from plex_playlist_sync.quality_defaults import entry_qualities, is_v2_items, normalize_entries
from plex_playlist_sync.seed_rules import apply_seed_rules_at_grab
from plex_playlist_sync.storage import Database

logger = logging.getLogger(__name__)


def candidate_rank(
    cand: AcquisitionSearchResult, res: EvaluationResult, preferred_protocol: Optional[str] = None
) -> tuple[Any, ...]:
    """``rank_key`` for a search result (seeders only count for torrents)."""
    is_torrent = cand.protocol == "torrent" or cand.source in ("torznab", "torrent") or bool(cand.magnet_url)
    return rank_key(cand.protocol, int(cand.seeders or 0) if is_torrent else 0, res, preferred_protocol)


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

    raw_items = data.get("items", []) or []
    entries = normalize_entries(raw_items) if is_v2_items(raw_items) else []
    items: list[QualityProfileItem] = []
    if entries:
        # v2 entries carry no weights; derive descending ones so legacy readers keep a consistent order.
        flat = [q for e in entries for q in entry_qualities(e)]
        allowed = {q: bool(e.get("allowed", True)) for e in entries for q in entry_qualities(e)}
        for idx, quality in enumerate(flat):
            items.append(QualityProfileItem(quality=quality, allowed=allowed[quality], weight=(len(flat) - idx) * 100))
    else:
        for item in raw_items:
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
        upgrade_allowed=bool(data.get("upgrade_allowed", True)),
        entries=entries,
        format_items=list(data.get("format_items") or []),
        min_format_score=int(data.get("min_format_score") or 0),
        cutoff_format_score=int(data.get("cutoff_format_score") or 0),
        min_upgrade_format_score=int(data.get("min_upgrade_format_score") or 1),
        catalog=dict(data.get("catalog") or {}),
    )


def resolve_duration(
    db: Database, album_id: Optional[str], track_id: Optional[str], item_type: Optional[str]
) -> Optional[DurationInfo]:
    """Duration a release should be measured against: the track for track searches, the album otherwise."""
    try:
        if item_type == "track":
            if track_id:
                seconds = db.get_track_duration(str(track_id))
                return DurationInfo(seconds, estimated=False, source="tracks") if seconds else None
            return None
        if album_id:
            durations, total = db.get_album_track_durations(str(album_id))
            return resolve_durations(durations, total)
    except sqlite3.Error as e:
        logger.warning("Could not resolve release duration for album %s / track %s: %s", album_id, track_id, e)
    return None


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
                    for r in res:
                        if isinstance(r.extra, dict) or r.extra is None:
                            extra = dict(r.extra or {})
                            extra.setdefault("indexer_id", idx_cfg.get("id"))
                            extra.setdefault("indexer_name", idx_name)
                            extra.setdefault("indexer_minimum_seeders", idx_cfg.get("minimum_seeders"))
                            r.extra = extra
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
        album_id: Optional[str] = None,
        track_id: Optional[str] = None,
        item_type: Optional[str] = None,
        preferred_protocol: Optional[str] = None,
        artist_tags: Optional[list[str]] = None,
    ) -> list[tuple[AcquisitionSearchResult, EvaluationResult]]:
        """Evaluates candidates against the QualityProfile and ranks the acceptable ones.

        Order: quality > custom-format score > distance to the preferred kbps > protocol preference > seeders.
        ``preferred_protocol`` (from the applicable delay profile) puts that protocol first in the preference.
        ``artist_tags`` (labels of the artist searched for) scope tag-restricted release profiles.
        ``album_id`` / ``track_id`` give the duration the quality-definition kbps limits are measured against.
        """
        if not candidates:
            return []

        prof = _to_quality_profile(profile)
        prepared = prepare_profile(prof)
        duration = resolve_duration(db, album_id, track_id, item_type) if db is not None else None
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
            eval_res = evaluate_prepared(
                parsed,
                prepared,
                candidate.size_bytes if candidate.size_bytes > 0 else None,
                duration=duration,
                artist_tags=artist_tags,
                **candidate_context(candidate),
            )
            if eval_res.is_acceptable:
                ranked.append((candidate, eval_res))

        ranked.sort(key=lambda item: candidate_rank(item[0], item[1], preferred_protocol), reverse=True)
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
        bypass_delay: bool = False,
        replacement_issue_id: Optional[str] = None,
        *,
        trigger: GrabTrigger,
    ) -> dict[str, Any]:
        """Searches indexers, ranks releases against the Quality Profile, and dispatches grab.

        The best release goes through the delay-profile gate: when its protocol has a delay it is parked in
        ``pending_releases`` (result ``{"success": False, "delayed": True, ...}``) unless a bypass applies or
        ``bypass_delay`` is set.

        Records active download transfer in the database upon successful dispatch. Runs under the native
        library-manager guard: if Lidarr manages the library it does nothing and reports ``mode_changed``.

        ``replacement_issue_id`` marks the grab as an admin replacement for a media issue: the grab history row names
        the issue so the import can comment on it. The blocklist still applies.

        ``trigger`` (required) records why the grab happened; it is kept with a delayed (parked) grab until release.
        """
        if db is None:
            raise ValueError("Database instance must be provided to search_and_grab")
        try:
            with work_guard(db, MODE_NATIVE):
                return self._search_and_grab(
                    artist, title, album, item_type, request_id, db, quality_profile_id, min_score, track_id, album_id,
                    bypass_delay, replacement_issue_id, trigger,
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
        bypass_delay: bool,
        replacement_issue_id: Optional[str],
        trigger: GrabTrigger,
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

        # 3. Evaluate and rank (protocol preference comes from the delay profile that applies to this artist)
        tags = delay_gate.artist_tags(db, artist)
        delay_profile = delay_gate.resolve_delay_profile(db, artist, tags=tags)
        preferred_protocol = delay_profile.get("preferred_protocol")
        ranked = self.evaluate_and_rank(
            candidates=candidates,
            profile=profile,
            db=db,
            album_id=album_id,
            track_id=track_id,
            item_type=item_type,
            preferred_protocol=preferred_protocol,
            artist_tags=tags,
        )
        if min_score is not None:
            ranked = [item for item in ranked if item[1].score > min_score]
        if not ranked:
            reason = (
                "No acceptable releases found meeting quality profile criteria"
                if min_score is None
                else f"No candidate release score exceeds current score {min_score}"
            )
            searched_kwargs: dict[str, Any] = dict(
                request_id=request_id, message=reason, dedupe_last=True,
                details={"reason": reason, "candidates_count": len(candidates), "searched": title},
                **trigger_kwargs(trigger),
            )
            if track_id or album_id:
                emit(db, "searched", track_id=track_id, album_id=album_id, **searched_kwargs)
            else:
                emit_named(
                    db, "searched", artist=artist, album=album or (title if item_type == "album" else None),
                    title=title if item_type != "album" else None, **searched_kwargs,
                )
            return {
                "success": False,
                "message": reason,
                "candidates_count": len(candidates),
            }

        top_candidate, eval_res = ranked[0]

        # 3b. Delay gate (manual grabs never come through here with a delay: they pass bypass_delay or use /grab)
        if not bypass_delay:
            decision = delay_gate.apply_gate(
                db,
                profile=delay_profile,
                top_tier=delay_gate.highest_allowed_tier(prepare_profile(profile)),
                candidate=top_candidate,
                result=eval_res,
                rank=candidate_rank(top_candidate, eval_res, preferred_protocol),
                artist=artist,
                item_title=title,
                album=album,
                item_type=item_type,
                request_id=request_id,
                album_id=album_id,
                track_id=track_id,
                quality_profile_id=quality_profile_id,
                upgrade_floor=min_score,
                trigger=trigger,
            )
            if not decision.grab:
                pending = decision.pending or {}
                logger.info("Holding '%s' for '%s - %s': %s", top_candidate.title, artist, title, decision.reason)
                return {
                    "success": False,
                    "delayed": True,
                    "pending_id": pending.get("id"),
                    "release_at": pending.get("release_at"),
                    "message": f"{decision.reason}; releases at {pending.get('release_at')}",
                }

        claimed = decision.claimed if not bypass_delay else None
        grabbed = False
        try:
            result = self.grab_candidate(
                db,
                top_candidate,
                parsed_quality=eval_res.parsed_quality,
                score=eval_res.score,
                artist=artist,
                album=album,
                item_type=item_type,
                request_id=request_id,
                track_id=track_id,
                album_id=album_id,
                upgrade=min_score is not None,
                replacement_issue_id=replacement_issue_id,
                trigger=trigger,
            )
            grabbed = bool(result.get("success"))
            return result
        finally:
            if claimed is not None and not grabbed:  # the gate claimed the parked row; a failed grab must not lose it
                db.restore_pending_release(claimed)

    def grab_candidate(
        self,
        db: Database,
        top_candidate: AcquisitionSearchResult,
        *,
        parsed_quality: Optional[str],
        score: int,
        artist: str,
        album: Optional[str],
        item_type: str,
        request_id: Optional[str],
        track_id: Optional[str],
        album_id: Optional[str],
        upgrade: bool = False,
        replacement_issue_id: Optional[str] = None,
        trigger: GrabTrigger,
    ) -> dict[str, Any]:
        """Dispatches a chosen candidate to its protocol's client and records the download (no delay gate)."""
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
                    db.set_request_outcome(request_id, e.reason, str(e) or None, schedule=False)  # backlog sweep retries it
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
        apply_seed_rules_at_grab(
            db, client_driver, download_id, download_hash, top_candidate.title, top_candidate.protocol,
            top_candidate.extra,
        )
        try:  # any grab of the item supersedes whatever is parked for it, under any of its identifiers
            db.clear_pending_for_item(request_id, album_id, track_id)
        except sqlite3.Error as clear_err:
            logger.warning("Failed to clear pending releases for '%s': %s", top_candidate.title, type(clear_err).__name__)
        try:
            db.record_download_grab(
                download_id,
                indexer=str((top_candidate.extra or {}).get("indexer_name") or top_candidate.source or "") or None,
                quality=parsed_quality,
                protocol=top_candidate.protocol or None,
                upgrade=upgrade,
                replacement_issue_id=replacement_issue_id,
                trigger=trigger,
            )
        except sqlite3.Error as hist_err:
            logger.warning("Failed to record grab history for %s: %s", download_id, type(hist_err).__name__)

        req_row = db.get_request(request_id) if request_id else None
        try:
            notification_dispatcher.dispatch(
                NotificationEvent.DOWNLOAD_STARTED,
                data={
                    "artist": artist,
                    "title": top_candidate.title,
                    "album": album,
                    "item_type": item_type,
                    "request_id": request_id,
                    "user_id": req_row.get("user_id") if req_row else None,
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
            score,
        )

        return {
            "success": True,
            "download_id": download_id,
            "download_hash": download_hash,
            "release": top_candidate.title,
            "client": client["name"],
            "score": score,
        }


# Module singleton
acquisition_coordinator = AcquisitionCoordinator()

__all__ = ["AcquisitionCoordinator", "acquisition_coordinator", "_to_quality_profile"]

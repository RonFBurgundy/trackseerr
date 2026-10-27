"""Automated Search & Grab Coordinator for TrackSeerr Phase 3.

Coordinates multi-indexer searches across Torznab, Newznab, and slskd,
scores and ranks candidates against Quality Profiles, dispatches grabs to
appropriate download clients (qBittorrent, SABnzbd, slskd), and tracks active transfers.
"""

import dataclasses
from dataclasses import dataclass
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Callable, Optional, Union
import uuid
from datetime import datetime, timezone

import httpx

from trackseerr.clients.acquisition import (
    get_acquisition_driver,
    get_indexer_driver,
    is_torrent_driver_type,
)
from trackseerr.clients.acquisition.base import AcquisitionRetryableError, AcquisitionUnavailableError
from trackseerr.clients.acquisition.slskd import SlskdNotLoggedInError
from trackseerr.item_history import GrabTrigger, emit, emit_named, trigger_kwargs
from trackseerr.library_manager import MODE_NATIVE, ModeChanged, work_guard
from trackseerr.redaction import redact_text
from trackseerr.search_terms import (
    banned_term_queries,
    build_query_ladder,
    matches_request,
    primary_artist,
    soulseek_file_matches_track,
    title_core,
)
from trackseerr.models import (
    AcquisitionSearchResult,
    ActiveDownload,
    DownloadClientConfig,
    DownloadStatus,
    EvaluationResult,
    NotificationEvent,
    QualityProfile,
    QualityProfileItem,
)
from trackseerr.notifications import notification_dispatcher
from trackseerr.decision_engine import (
    DurationInfo,
    candidate_context,
    evaluate_prepared,
    prepare_profile,
    DEFAULT_INDEXER_PRIORITY,
    phrase_score,
    rank_key,
    resolve_durations,
)
from trackseerr import delay_gate, quality_unavailable
from trackseerr.quality import evaluate_release, parse_release_title
from trackseerr.quality_defaults import entry_qualities, is_v2_items, normalize_entries
from trackseerr.seed_rules import apply_seed_rules_at_grab
from trackseerr.storage import Database

logger = logging.getLogger(__name__)


def _slskd_extra(cand: AcquisitionSearchResult) -> dict[str, Any]:
    return (cand.extra or {}) if cand.protocol == "slskd" else {}


def _peer_rank(cand: AcquisitionSearchResult) -> tuple[int, ...]:
    """Soulseek peer availability: free slot first, then shortest queue, then fastest upload."""
    peer = _slskd_extra(cand).get("peer")
    if not isinstance(peer, dict):
        return ()
    try:
        queue = int(peer.get("queue_length") or 0)
        speed = int(peer.get("upload_speed") or 0)
    except (TypeError, ValueError):
        return ()
    return (1 if peer.get("has_free_slot") else 0, -queue, speed)


def _length_mismatch(
    cand: AcquisitionSearchResult, duration: Optional[DurationInfo], item_type: Optional[str]
) -> Optional[str]:
    """Reason a Soulseek file's length is too far from the expected track length, else ``None``."""
    if item_type != "track" or duration is None or duration.estimated:
        return None
    actual = _slskd_extra(cand).get("length_seconds")
    if isinstance(actual, bool) or not isinstance(actual, int) or actual <= 0:
        return None
    expected = duration.seconds
    if abs(actual - expected) <= max(15.0, 0.10 * expected):
        return None
    return f"length {actual}s vs expected {expected:.0f}s"


def _slskd_kind_mismatch(cand: AcquisitionSearchResult, item_type: Optional[str]) -> bool:
    """True when a Soulseek result is the wrong kind: album requests take folders, everything else takes files."""
    if cand.protocol != "slskd":
        return False
    return cand.item_type != ("album" if item_type == "album" else "track")


def _folder_mismatch(
    cand: AcquisitionSearchResult, tracks: list[tuple[int, int, str]], duration: Optional[DurationInfo]
) -> Optional[str]:
    """Reason a Soulseek album folder does not match the album's tracklist, else ``None``."""
    if cand.protocol != "slskd" or cand.item_type != "album" or not tracks:
        return None
    extra = _slskd_extra(cand)
    wanted = len(tracks)
    count = extra.get("track_count")
    count = count if isinstance(count, int) and not isinstance(count, bool) else 0
    if count < wanted or count > wanted + 2:
        return f"folder has {count} tracks, album has {wanted}"
    if len({disc for disc, _, _ in tracks}) > 1:
        return "multi-disc album; Soulseek folder grabs are single-disc only"
    names = []
    for f in extra.get("files") or []:
        if isinstance(f, dict):
            names.append(str(f.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1])
    found = sum(
        1 for _, _, title in tracks if any(phrase_score(title_core(title), name) >= 80 for name in names)
    )
    if found < 0.8 * wanted:
        return f"only {found}/{wanted} track titles found in folder"
    total = extra.get("total_length_seconds")
    if duration is not None and not duration.estimated and isinstance(total, int) and not isinstance(total, bool):
        if abs(total - duration.seconds) > max(30.0, 0.05 * duration.seconds):
            return f"folder length {total}s vs album {duration.seconds:.0f}s"
    return None


def _skip_reason(
    cand: AcquisitionSearchResult,
    item_type: Optional[str],
    duration: Optional[DurationInfo],
    tracks: list[tuple[int, int, str]],
) -> Optional[str]:
    """Why a Soulseek candidate must be skipped (wrong kind, folder off the tracklist, wrong length), else ``None``."""
    if _slskd_kind_mismatch(cand, item_type):
        return f"{cand.item_type} result does not fit a {item_type or 'track'} request"
    return _folder_mismatch(cand, tracks, duration) or _length_mismatch(cand, duration, item_type)


def candidate_rank(
    cand: AcquisitionSearchResult, res: EvaluationResult, preferred_protocol: Optional[str] = None
) -> tuple[Any, ...]:
    """``rank_key`` for a search result (seeders only count for torrents)."""
    is_torrent = cand.protocol == "torrent" or cand.source in ("torznab", "torrent") or bool(cand.magnet_url)
    raw_priority = (cand.extra or {}).get("indexer_priority")
    indexer_priority = raw_priority if isinstance(raw_priority, int) and not isinstance(raw_priority, bool) else None
    return rank_key(
        cand.protocol,
        int(cand.seeders or 0) if is_torrent else 0,
        res,
        preferred_protocol,
        indexer_priority,
        peer=_peer_rank(cand),
    )


CLIENT_BACKOFF_SECONDS = 300.0


class NoClientAvailable(RuntimeError):
    """No enabled download client can take a release of the requested protocol."""


def _protocol_group(protocol: str) -> Optional[str]:
    """Maps a protocol/source label to the client family that serves it (``torrent``/``usenet``/``slskd``)."""
    proto = str(protocol).lower().strip()
    if proto in ("torrent", "torznab"):
        return "torrent"
    if proto in ("usenet", "newznab"):
        return "usenet"
    if proto in ("slskd", "soulseek", "p2p"):
        return "slskd"
    return None


def _client_serves(group: str, client: dict[str, Any]) -> bool:
    driver_type = str(client.get("driver_type", "")).lower()
    if group == "torrent":
        return is_torrent_driver_type(client.get("driver_type"))
    if group == "usenet":
        return driver_type in ("sabnzbd", "nzbget")
    return driver_type == "slskd"


def search_unavailable_reason(db: Any) -> Optional[str]:
    """Returns None when search and grab are viable, or a human explanation when not.

    Viable means:
      - At least one enabled slskd download client is configured (slskd searches and grabs itself), OR
      - At least one enabled indexer has a matching enabled download client serving its protocol group.

    Fails open (returns None) on sqlite3.Error reading tables to avoid silently stopping acquisition on DB hiccups.
    """
    try:
        indexers = db.list_indexers(enabled_only=True)
        clients = db.list_download_clients(enabled_only=True)
    except sqlite3.Error as e:
        logger.warning("search_unavailable_reason: DB error querying indexers/clients, failing open: %s", e)
        return None

    # Check for enabled slskd client first (slskd can search and grab itself)
    if any(_client_serves("slskd", c) for c in clients):
        return None

    groups: set[str] = set()
    for idx in indexers:
        idx_type = idx.get("indexer_type") or "torznab"
        grp = _protocol_group(idx_type)
        if grp:
            groups.add(grp)

    if not groups:
        return "no indexers or Soulseek client configured"

    viable_groups = {grp for grp in groups if any(_client_serves(grp, c) for c in clients)}
    if viable_groups:
        return None

    # No indexer protocol has a matching client
    has_torrent = "torrent" in groups
    has_usenet = "usenet" in groups
    if has_torrent and has_usenet:
        return "no download client for torrent or usenet indexers"
    if has_torrent:
        return "no download client for torrent indexers"
    if has_usenet:
        return "no download client for usenet indexers"
    return "no indexers or Soulseek client configured"


def _has_viable_indexer(indexers: list[dict[str, Any]], clients: list[dict[str, Any]]) -> bool:
    groups = {grp for idx in indexers if (grp := _protocol_group(idx.get("indexer_type") or "torznab"))}
    return any(any(_client_serves(grp, c) for c in clients) for grp in groups)


def _notify_soulseek_reconnected(db: Any) -> None:
    global _OUTAGE_NOTIFIED
    with _SOULSEEK_LOCK:
        if not _OUTAGE_NOTIFIED:
            return
        _OUTAGE_NOTIFIED = False
    try:
        db.record_event(
            "soulseek_reconnected",
            "Soulseek is connected again. Searches resumed.",
            severity="info",
            source="AcquisitionCoordinator",
        )
    except (sqlite3.Error, ValueError) as ev_err:
        logger.warning("Failed to record soulseek_reconnected event: %s", ev_err)


def _notify_soulseek_disconnected(db: Any) -> None:
    global _OUTAGE_NOTIFIED
    with _SOULSEEK_LOCK:
        if _OUTAGE_NOTIFIED:
            return
        _OUTAGE_NOTIFIED = True
    try:
        db.record_event(
            "soulseek_disconnected",
            "Soulseek is not connected. Searches are paused until slskd logs back in.",
            source="AcquisitionCoordinator",
            severity="warning",
        )
    except (sqlite3.Error, ValueError) as ev_err:
        logger.warning("Failed to record soulseek_disconnected event: %s", ev_err)

    try:
        notification_dispatcher.dispatch(
            NotificationEvent.SOURCE_UNAVAILABLE,
            data={"source": "Soulseek", "reason": "slskd is not logged in"},
            db=db,
        )
    except (sqlite3.Error, ValueError) as ev_err:
        logger.warning("Failed to dispatch source_unavailable notification: %s", ev_err)


def _reconnect_slskd_clients(drivers: list[tuple[dict[str, Any], Any]]) -> None:
    now = time.monotonic()
    for s_cfg, driver in drivers:
        name = str(s_cfg.get("name") or s_cfg.get("id") or "slskd")
        with _SOULSEEK_LOCK:
            last_reconn = _SOULSEEK_RECONNECT_TIMESTAMPS.get(name, 0.0)
            can_reconn = (now - last_reconn) >= 600.0
            if can_reconn:
                _SOULSEEK_RECONNECT_TIMESTAMPS[name] = now
        if can_reconn:
            driver.reconnect()


def soulseek_offline_reason(db: Any) -> Optional[str]:
    """Returns a reason string if Soulseek is disconnected and no indexer alternatives exist, else None."""
    try:
        indexers = db.list_indexers(enabled_only=True)
        clients = db.list_download_clients(enabled_only=True)
    except sqlite3.Error as e:
        logger.warning("soulseek_offline_reason: DB error querying indexers/clients, failing open: %s", e)
        return None

    if _has_viable_indexer(indexers, clients):
        return None

    slskd_clients = [c for c in clients if _client_serves("slskd", c)]
    if not slskd_clients:
        return None

    drivers = []
    for s_cfg in slskd_clients:
        driver = get_acquisition_driver(s_cfg)
        status = driver.server_logged_in()
        if status is None:
            return None
        if status is True:
            _notify_soulseek_reconnected(db)
            return None
        drivers.append((s_cfg, driver))

    _reconnect_slskd_clients(drivers)

    if any(driver.server_logged_in() is True for _, driver in drivers):
        _notify_soulseek_reconnected(db)
        return None

    _notify_soulseek_disconnected(db)
    return "Soulseek is not connected (slskd is not logged in)"


def _client_priority(client: dict[str, Any]) -> int:
    try:
        return int(client.get("priority") or 1)
    except (TypeError, ValueError):
        return 1


def _indexer_priority(cfg: dict[str, Any]) -> int:
    try:
        return int(cfg.get("priority") or DEFAULT_INDEXER_PRIORITY)
    except (TypeError, ValueError):
        return DEFAULT_INDEXER_PRIORITY


_BTIH_RE = re.compile(r"urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", re.IGNORECASE)


def _apply_blocklist(
    db: Database, candidates: list[AcquisitionSearchResult], include_blocklisted: bool
) -> list[AcquisitionSearchResult]:
    """Drops blocklisted candidates, or keeps them flagged ``extra["blocklisted"]`` when ``include_blocklisted``."""
    kept: list[AcquisitionSearchResult] = []
    for candidate in candidates:
        entry = db.find_blocklist_entry(
            release_title=candidate.title,
            release_guid=candidate.download_id,
            info_hash=_extract_info_hash(candidate),
        )
        if entry is not None:
            if not include_blocklisted:
                logger.info("Skipping blocklisted release candidate during search: '%s'", candidate.title)
                continue
            candidate.extra = {
                **(candidate.extra or {}),
                "blocklisted": True,
                "blocklist": {
                    "reason": entry.get("reason"),
                    "indexer": entry.get("indexer"),
                    "created_at": entry.get("created_at"),
                },
            }
        kept.append(candidate)
    return kept


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


@dataclass(frozen=True)
class SearchContext:
    """What triggered a search, persisted with each search_log row."""

    trigger_kind: Optional[str] = None
    trigger_ref: Optional[str] = None
    trigger_label: Optional[str] = None
    request_id: Optional[str] = None
    track_id: Optional[str] = None
    album_id: Optional[str] = None
    rung: int = 1


@dataclass
class SearchStats:
    """Accumulates indexer search counts for fallback tier decisions."""

    slskd_searched: int = 0
    slskd_responses: int = 0
    slskd_offline: bool = False


def _record_search(
    db: Database,
    context: Optional[SearchContext],
    source_name: str,
    source_type: Optional[str],
    artist: str,
    title: Optional[str],
    album: Optional[str],
    count: int,
    started: float,
    error: Optional[str] = None,
) -> None:
    """Persists one search_log row; a storage failure is logged and never breaks the search."""
    ctx = context or SearchContext()
    try:
        db.record_search(
            source_name=str(source_name),
            source_type=source_type or "torznab",
            query_artist=artist,
            query_title=title,
            query_album=album,
            rung=ctx.rung,
            result_count=count,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=error,
            trigger_kind=ctx.trigger_kind,
            trigger_ref=ctx.trigger_ref,
            trigger_label=ctx.trigger_label,
            request_id=ctx.request_id,
            track_id=ctx.track_id,
            album_id=ctx.album_id,
        )
    except sqlite3.Error as e:
        logger.warning("Could not record search for source %s: %s", source_name, e)


def _log_source_search(
    source: str,
    source_type: Optional[str],
    artist: str,
    title: Optional[str],
    album: Optional[str],
    results: Optional[list[AcquisitionSearchResult]],
    started: float,
) -> None:
    logger.info(
        "Indexer search source=%s type=%s query_artist=%r query_title=%r query_album=%r -> %d results in %.1fs",
        source, source_type or "indexer", artist, title, album, len(results or []), time.monotonic() - started,
    )


def _exclude_gated_slskd_peers(db: Database, driver: Any, s_cfg: dict, s_name: str) -> None:
    """Add gated slskd peers to the driver's ignored users; log on DB errors."""
    try:
        gated = db.excluded_slskd_peers(str(s_cfg.get("id")), datetime.now(timezone.utc).isoformat())
        driver.ignored_users = set(driver.ignored_users) | gated
    except sqlite3.Error as exc:
        logger.warning("Could not read gated slskd peers for '%s': %s", s_name, redact_text(str(exc)))


_BANNED_ARTIST_TTL = 7 * 86400.0
_BANNED_TIER_MISS_TTL = 24 * 3600.0

_BANNED_CACHE_LOCK = threading.Lock()
_BANNED_ARTISTS: dict[str, float] = {}
_BANNED_TIER_MISSES: dict[tuple[str, str], float] = {}

_SOULSEEK_LOCK = threading.RLock()
_SOULSEEK_OFFLINE: bool = False
_OUTAGE_NOTIFIED: bool = False
_SOULSEEK_RECONNECT_TIMESTAMPS: dict[str, float] = {}


def _set_soulseek_offline(value: bool) -> None:
    global _SOULSEEK_OFFLINE
    with _SOULSEEK_LOCK:
        _SOULSEEK_OFFLINE = value


def soulseek_offline_flagged() -> bool:
    with _SOULSEEK_LOCK:
        return _SOULSEEK_OFFLINE


def _artist_banned(artist: str) -> bool:
    key = primary_artist(artist).strip().lower()
    if not key:
        return False
    now = time.monotonic()
    with _BANNED_CACHE_LOCK:
        expiry = _BANNED_ARTISTS.get(key)
        if expiry is not None:
            if now < expiry:
                return True
            del _BANNED_ARTISTS[key]
        return False


def _mark_artist_banned(artist: str) -> None:
    key = primary_artist(artist).strip().lower()
    if key:
        with _BANNED_CACHE_LOCK:
            _BANNED_ARTISTS[key] = time.monotonic() + _BANNED_ARTIST_TTL


def _tier_recently_missed(key: tuple[str, str]) -> bool:
    norm_key = (key[0].strip().lower(), key[1].strip().lower())
    now = time.monotonic()
    with _BANNED_CACHE_LOCK:
        expiry = _BANNED_TIER_MISSES.get(norm_key)
        if expiry is not None:
            if now < expiry:
                return True
            del _BANNED_TIER_MISSES[norm_key]
        return False


def _mark_tier_missed(key: tuple[str, str]) -> None:
    norm_key = (key[0].strip().lower(), key[1].strip().lower())
    with _BANNED_CACHE_LOCK:
        _BANNED_TIER_MISSES[norm_key] = time.monotonic() + _BANNED_TIER_MISS_TTL


def _search_indexers(
    db: Database,
    artist: str,
    title: Optional[str],
    album: Optional[str],
    context: Optional[SearchContext],
    errors: Optional[list[dict[str, str]]],
) -> list[AcquisitionSearchResult]:
    try:
        indexers = db.list_indexers(enabled_only=True)
    except Exception as e:
        logger.error("Failed to query enabled indexers from database: %s", e)
        indexers = []

    results: list[AcquisitionSearchResult] = []
    for idx_cfg in indexers:
        idx_name = idx_cfg.get("name", "unknown")
        idx_type = idx_cfg.get("indexer_type")
        started = time.monotonic()
        try:
            driver = get_indexer_driver(idx_cfg)
            started = time.monotonic()
            res = driver.search(artist=artist, title=title, album=album)
            _log_source_search(idx_name, idx_type, artist, title, album, res, started)
            _record_search(db, context, idx_name, idx_type, artist, title, album, len(res or []), started)
            if res:
                for r in res:
                    if isinstance(r.extra, dict) or r.extra is None:
                        extra = dict(r.extra or {})
                        extra.setdefault("indexer_id", idx_cfg.get("id"))
                        extra.setdefault("indexer_name", idx_name)
                        extra.setdefault("indexer_priority", _indexer_priority(idx_cfg))
                        extra.setdefault("indexer_minimum_seeders", idx_cfg.get("minimum_seeders"))
                        r.extra = extra
                results.extend(res)
        except Exception as e:
            logger.warning("Error searching indexer '%s' (%s): %s", idx_name, idx_cfg.get("host_url"), e)
            _record_search(db, context, idx_name, idx_type, artist, title, album, 0, started, redact_text(str(e)))
            if errors is not None:
                errors.append({"source": str(idx_name), "error": redact_text(str(e))})
    return results


def _search_slskd(
    db: Database,
    artist: str,
    title: Optional[str],
    album: Optional[str],
    context: Optional[SearchContext],
    errors: Optional[list[dict[str, str]]],
    slskd_query_text: Optional[str],
    stats: Optional[SearchStats],
) -> list[AcquisitionSearchResult]:
    try:
        download_clients = db.list_download_clients(enabled_only=True)
    except Exception as e:
        logger.error("Failed to query download clients from database: %s", e)
        download_clients = []

    slskd_clients = [
        c for c in download_clients
        if str(c.get("driver_type", "")).lower() == "slskd"
    ]
    results: list[AcquisitionSearchResult] = []
    for s_cfg in slskd_clients:
        s_name = s_cfg.get("name", "slskd")
        started = time.monotonic()
        log_artist = "" if slskd_query_text else artist
        log_title = slskd_query_text if slskd_query_text else title
        log_album = None if slskd_query_text else album
        try:
            driver = get_acquisition_driver(s_cfg)
            _exclude_gated_slskd_peers(db, driver, s_cfg, s_name)
            started = time.monotonic()
            if slskd_query_text:
                res = driver.search(artist=artist, title=title, album=album, query_text=slskd_query_text)
            else:
                res = driver.search(artist=artist, title=title, album=album)
            _set_soulseek_offline(False)
            if stats is not None:
                stats.slskd_searched += 1
                stats.slskd_responses += getattr(driver, "last_response_count", 0)
            _log_source_search(s_name, "slskd", log_artist, log_title, log_album, res, started)
            _record_search(db, context, s_name, "slskd", log_artist, log_title, log_album, len(res or []), started)
            if res:
                for r in res:
                    extra = dict(r.extra or {})
                    extra["indexer_priority"] = _indexer_priority(s_cfg)
                    extra.setdefault("indexer_name", s_name)
                    r.extra = extra
                results.extend(res)
        except SlskdNotLoggedInError as e:
            if not soulseek_offline_flagged():
                logger.warning("Soulseek is offline / not logged in on '%s': %s", s_name, e)
            _set_soulseek_offline(True)
            if stats is not None:
                stats.slskd_offline = True
            _record_search(db, context, s_name, "slskd", log_artist, log_title, log_album, 0, started, redact_text(str(e)))
            if errors is not None:
                errors.append({"source": str(s_name), "error": redact_text(str(e))})
        except Exception as e:
            logger.warning("Error searching slskd client '%s' (%s): %s", s_name, s_cfg.get("host_url"), e)
            _record_search(db, context, s_name, "slskd", log_artist, log_title, log_album, 0, started, redact_text(str(e)))
            if errors is not None:
                errors.append({"source": str(s_name), "error": redact_text(str(e))})
    return results


class AcquisitionCoordinator:
    """Coordinates search, quality evaluation, client dispatch, and active transfer records."""

    @staticmethod
    def soulseek_offline_reason(db: Any) -> Optional[str]:
        return soulseek_offline_reason(db)

    @staticmethod
    def soulseek_offline_flagged() -> bool:
        return soulseek_offline_flagged()

    def search_all_indexers(
        self,
        artist: str,
        title: Optional[str] = None,
        album: Optional[str] = None,
        db: Optional[Database] = None,
        include_blocklisted: bool = False,
        errors: Optional[list[dict[str, str]]] = None,
        context: Optional[SearchContext] = None,
        slskd_only: bool = False,
        slskd_query_text: Optional[str] = None,
        stats: Optional[SearchStats] = None,
    ) -> list[AcquisitionSearchResult]:
        """Queries all enabled Torznab/Newznab indexers and slskd clients.

        Errors on individual indexers or clients are logged without aborting others; when ``errors`` is given each
        failure is also appended to it as ``{"source", "error"}`` (redacted). Blocklisted releases are dropped unless
        ``include_blocklisted`` is set, in which case they are kept with ``extra["blocklisted"] = True``.
        """
        if db is None:
            logger.warning("No database supplied to search_all_indexers")
            return []

        all_results: list[AcquisitionSearchResult] = []
        if not slskd_only:
            all_results.extend(_search_indexers(db, artist, title, album, context, errors))
        all_results.extend(_search_slskd(db, artist, title, album, context, errors, slskd_query_text, stats))
        return _apply_blocklist(db, all_results, include_blocklisted)

    @staticmethod
    def _candidate_text(result: AcquisitionSearchResult) -> str:
        """Result title plus the slskd filename (its path carries the artist folder) when present."""
        extra = result.extra
        if isinstance(extra, dict) and extra.get("filename"):
            return f"{result.title} {extra['filename']}"
        return result.title

    @staticmethod
    def _is_slskd_file(result: AcquisitionSearchResult) -> bool:
        """True for a Soulseek single-file result, whose title is the file name rather than a release name."""
        return result.protocol == "slskd" and result.item_type == "track"

    def _relevant(
        self,
        results: list[AcquisitionSearchResult],
        number: int,
        artist: str,
        title: Optional[str],
        album: Optional[str],
    ) -> list[AcquisitionSearchResult]:
        """Relevance filter for one rung. Rung 1 only filters Soulseek files of a track request; later rungs filter all."""
        if number == 1 and not title:
            return results
        kept = []
        for r in results:
            slskd_file = self._is_slskd_file(r)
            if number == 1 and not slskd_file:
                kept.append(r)
                continue
            if not matches_request(
                self._candidate_text(r),
                artist,
                title,
                album,
                title_text=r.title if slskd_file else None,
            ):
                continue
            if slskd_file and title and not soulseek_file_matches_track(r.title, title):
                continue
            kept.append(r)
        return kept

    def _banned_term_tier(
        self,
        artist: str,
        title: Optional[str],
        album: Optional[str],
        db: Optional[Database],
        context: Optional[SearchContext] = None,
        start_rung: int = 4,
    ) -> list[AcquisitionSearchResult]:
        miss_key = (artist, title or album or "")
        if _tier_recently_missed(miss_key):
            return []

        queries = banned_term_queries(artist, title, album)
        for i, text in enumerate(queries):
            ctx = dataclasses.replace(context or SearchContext(), rung=start_rung + i)
            stats = SearchStats()
            results = self.search_all_indexers(
                artist=artist,
                title=title,
                album=album,
                db=db,
                context=ctx,
                slskd_only=True,
                slskd_query_text=text,
                stats=stats,
            )
            if stats.slskd_offline:
                return []
            kept = self._relevant(results, 2, artist, title, album)
            if kept:
                _mark_artist_banned(artist)
                logger.info(
                    "Soulseek banned-term search found %d results for %r - %r via %r",
                    len(kept),
                    artist,
                    title or album,
                    text,
                )
                return kept

        _mark_tier_missed(miss_key)
        return []

    def search_with_fallback(
        self,
        artist: str,
        title: Optional[str],
        album: Optional[str],
        db: Optional[Database],
        context: Optional[SearchContext] = None,
    ) -> list[AcquisitionSearchResult]:
        """Searches with the raw query, then simpler rungs, returning the first non-empty result list.

        Rung 1 is unfiltered except Soulseek files of a track request (matched by file name); later rungs keep only results that pass ``matches_request``
        against the ORIGINAL title/album so a loose query cannot grab the wrong release.
        """
        if _artist_banned(artist):
            tier_results = self._banned_term_tier(artist, title, album, db, context, start_rung=1)
            if tier_results:
                return tier_results

        rungs = build_query_ladder(artist, title, album)
        rung1_stats = SearchStats()
        for number, (r_artist, r_title, r_album) in enumerate(rungs, start=1):
            started = time.monotonic()
            stats_arg = rung1_stats if number == 1 else SearchStats()
            results = self.search_all_indexers(
                artist=r_artist,
                title=r_title,
                album=r_album,
                db=db,
                context=dataclasses.replace(context or SearchContext(), rung=number),
                stats=stats_arg,
            )
            found = len(results)
            results = self._relevant(results, number, artist, title, album)
            logger.info(
                "Search rung %d/%d artist=%r title=%r album=%r -> %d results (%d after relevance) in %.1fs",
                number, len(rungs), r_artist, r_title, r_album, found, len(results), time.monotonic() - started,
            )
            if results:
                return results
            if stats_arg.slskd_offline:
                logger.info("Search stopped for %r - %r: Soulseek is not connected", artist, title or album)
                return []

        if rung1_stats.slskd_searched > 0 and rung1_stats.slskd_responses == 0:
            tier_results = self._banned_term_tier(artist, title, album, db, context, start_rung=len(rungs) + 1)
            if tier_results:
                return tier_results

        logger.info("Search exhausted %d rungs for %r - %r with no results", len(rungs), artist, title or album)
        return []

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
        tracks: list[tuple[int, int, str]] = []
        if item_type == "album" and album_id and db is not None:
            try:
                tracks = db.get_album_track_titles(str(album_id))
            except sqlite3.Error as e:
                logger.warning("Could not load tracklist for album %s: %s", album_id, e)
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

            skip = _skip_reason(candidate, item_type, duration, tracks)
            if skip:
                if _slskd_kind_mismatch(candidate, item_type):
                    logger.debug("Skipping Soulseek result '%s': %s", candidate.title, skip)
                elif candidate.item_type == "album":
                    logger.info("Skipping Soulseek folder '%s': %s", candidate.title, skip)
                else:
                    logger.info("Skipping Soulseek file '%s': %s", candidate.title, skip)
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

    def __init__(self) -> None:
        self._client_lock = threading.Lock()
        self._rotation: dict[tuple[str, int], int] = {}
        self._client_backoff: dict[str, float] = {}

    def _mark_client_failed(self, client: dict[str, Any], reason: str) -> None:
        """Skips the client for ``CLIENT_BACKOFF_SECONDS`` after a client-side dispatch failure."""
        with self._client_lock:
            self._client_backoff[str(client.get("id"))] = time.monotonic() + CLIENT_BACKOFF_SECONDS
        logger.warning(
            "Download client '%s' failed a dispatch (%s); trying other clients first for %d minutes",
            client.get("name"), reason, int(CLIENT_BACKOFF_SECONDS // 60),
        )

    def _client_available(self, client_id: Any) -> bool:
        with self._client_lock:
            deadline = self._client_backoff.get(str(client_id))
            if deadline is None:
                return True
            if time.monotonic() >= deadline:
                del self._client_backoff[str(client_id)]
                return True
            return False

    def clients_for_protocol(self, protocol: str, db: Database) -> list[dict[str, Any]]:
        """Enabled clients for the protocol in dispatch order.

        Ordered by priority ascending (1 = preferred); clients sharing a priority take turns (the head rotates on each
        call); clients in backoff after a failed dispatch go last rather than being dropped.

        - "torrent" / "torznab": driver_type in ("qbittorrent", "transmission", "deluge")
        - "usenet" / "newznab": driver_type in ("sabnzbd", "nzbget")
        - "slskd" / "soulseek" / "p2p": driver_type == "slskd"
        """
        group = _protocol_group(protocol)
        if group is None:
            return []
        try:
            enabled_clients = db.list_download_clients(enabled_only=True)
        except sqlite3.Error as e:
            logger.error("Failed to list download clients from database: %s", e)
            return []

        matching = [c for c in enabled_clients if _client_serves(group, c)]
        matching.sort(key=lambda c: (_client_priority(c), str(c.get("created_at", ""))))

        ordered: list[dict[str, Any]] = []
        priorities = sorted({_client_priority(c) for c in matching})
        with self._client_lock:
            for prio in priorities:
                tier = [c for c in matching if _client_priority(c) == prio]
                turn = self._rotation.get((group, prio), 0)
                self._rotation[(group, prio)] = turn + 1
                shift = turn % len(tier)
                ordered.extend(tier[shift:] + tier[:shift])
        available = [c for c in ordered if self._client_available(c.get("id"))]
        backed_off = [c for c in ordered if not self._client_available(c.get("id"))]
        return available + backed_off

    def find_client_for_protocol(self, protocol: str, db: Database) -> Optional[dict[str, Any]]:
        """The first client ``clients_for_protocol`` would try, or None."""
        clients = self.clients_for_protocol(protocol, db)
        return clients[0] if clients else None

    def dispatch_with_failover(
        self,
        db: Database,
        candidate: AcquisitionSearchResult,
        driver_factory: Optional[Callable[[dict[str, Any]], Any]] = None,
    ) -> tuple[dict[str, Any], str]:
        """Sends the candidate to the best client, falling over to the next on client-side failures.

        Returns ``(client, download_hash)`` for the client that accepted it. ``AcquisitionUnavailableError`` (the
        release itself is gone) is re-raised immediately; if every client fails the last error is re-raised;
        ``NoClientAvailable`` is raised when no client serves the protocol. ``driver_factory`` builds a client's driver
        (default ``get_acquisition_driver``).
        """
        make_driver = driver_factory or get_acquisition_driver
        clients = self.clients_for_protocol(candidate.protocol, db)
        if not clients:
            raise NoClientAvailable(f"No enabled download client available for {candidate.protocol}")
        last_error: Optional[Exception] = None
        for client in clients:
            try:
                return client, make_driver(client).download(candidate)
            except AcquisitionUnavailableError:
                raise
            except (AcquisitionRetryableError, httpx.HTTPError, OSError, RuntimeError, ValueError) as e:
                self._mark_client_failed(client, f"{type(e).__name__}: {redact_text(str(e))}")
                last_error = e
        assert last_error is not None
        raise last_error

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
        search_context = SearchContext(
            trigger_kind=trigger.kind,
            trigger_ref=trigger.ref,
            trigger_label=trigger.label,
            request_id=request_id,
            track_id=track_id,
            album_id=album_id,
        )
        candidates = self.search_with_fallback(artist, title, album, db, context=search_context)

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
            if min_score is None and candidates:
                quality_unavailable.maybe_notify(
                    db,
                    candidates=candidates,
                    profile=profile,
                    profile_name=profile_dict.get("name") or "default",
                    artist=artist,
                    title=title or album,
                    track_id=track_id,
                    album_id=album_id,
                    request_id=request_id,
                )
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
        # 5. Dispatch to the best client for the protocol, failing over to the next on client-side errors
        try:
            client, download_hash = self.dispatch_with_failover(db, top_candidate)
            client_driver = get_acquisition_driver(client)
        except NoClientAvailable:
            return {
                "success": False,
                "message": f"No enabled download client available for {top_candidate.protocol}",
            }
        except (AcquisitionRetryableError, AcquisitionUnavailableError) as e:
            retryable = isinstance(e, AcquisitionRetryableError)
            logger.warning(
                "Dispatch for '%s' %s: %s",
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
                "Dispatch download failed for '%s': %s",
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

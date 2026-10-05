"""Delay-profile selection and the delay gate (spec sections 5 and 6, step 7).

Ranking has already picked the best candidate for an item; the gate decides whether to grab it now or park it in
``pending_releases`` until ``release_at``. As in Lidarr the window is anchored on the first time the item was seen:
a better candidate arriving inside the window replaces the pending one but never restarts the clock.

Artist tags: ``library_artists`` has no tags column. Tags are read from ``metadata_json["tags"]`` when present (none are
written today), so in practice every artist uses the default delay profile until artist tagging exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import sqlite3
from typing import Any, Optional

from plex_playlist_sync.decision_engine import PreparedProfile, normalize_protocol
from plex_playlist_sync.models import AcquisitionSearchResult, EvaluationResult

logger = logging.getLogger(__name__)

_CANDIDATE_FIELDS = (
    "download_id",
    "title",
    "artist",
    "album",
    "item_type",
    "size_bytes",
    "bit_rate",
    "format",
    "quality_str",
    "seeders",
    "leechers",
    "download_url",
    "magnet_url",
    "source",
    "extra",
    "protocol",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------------------------------
# Profile selection
# --------------------------------------------------------------------------------------------------------------------


def artist_tags(db: Any, artist_name: Optional[str]) -> list[str]:
    """Tags of the library artist named ``artist_name`` (empty when unknown or the artist carries none)."""
    name = (artist_name or "").strip().lower()
    if not name:
        return []
    try:
        with db._lock:
            row = db.conn.execute(
                "SELECT metadata_json FROM library_artists WHERE clean_name = ? OR lower(name) = ? LIMIT 1",
                (name, name),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("Could not read artist tags for '%s': %s", artist_name, type(exc).__name__)
        return []
    if not row or not row[0]:
        return []
    try:
        meta = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return []
    tags = meta.get("tags") if isinstance(meta, dict) else None
    return [str(t) for t in tags] if isinstance(tags, list) else []


def select_delay_profile(profiles: list[dict[str, Any]], tags: list[str]) -> dict[str, Any]:
    """First profile (in ``order``) whose tags intersect ``tags``; otherwise the default profile."""
    wanted = {t.strip().lower() for t in tags if t and t.strip()}
    default: Optional[dict[str, Any]] = None
    for p in profiles:
        if p.get("is_default"):
            default = p
            continue
        if wanted and wanted & {str(t).strip().lower() for t in p.get("tags") or []}:
            return p
    return default or {
        "id": None,
        "name": "Default",
        "preferred_protocol": "usenet",
        "delays": {"usenet": 0, "torrent": 0, "soulseek": 0},
        "bypass_if_highest_quality": True,
        "bypass_if_above_score": None,
        "tags": [],
        "is_default": True,
    }


def resolve_delay_profile(db: Any, artist_name: Optional[str]) -> dict[str, Any]:
    try:
        profiles = db.list_delay_profiles()
    except sqlite3.Error as exc:
        logger.warning("Could not load delay profiles (%s); delays disabled for this decision", type(exc).__name__)
        profiles = []
    return select_delay_profile(profiles, artist_tags(db, artist_name))


def delay_minutes(profile: dict[str, Any], protocol: Optional[str]) -> int:
    proto = normalize_protocol(protocol)
    return max(0, int((profile.get("delays") or {}).get(proto, 0) or 0))


# --------------------------------------------------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------------------------------------------------


def highest_allowed_tier(prepared: PreparedProfile) -> Optional[int]:
    tiers = [idx for q, idx in prepared.tiers.items() if prepared.allowed.get(q)]
    return min(tiers) if tiers else None


def bypass_reason(
    profile: dict[str, Any], result: EvaluationResult, top_tier: Optional[int]
) -> Optional[str]:
    if (
        profile.get("bypass_if_highest_quality")
        and top_tier is not None
        and result.tier is not None
        and result.tier <= top_tier
    ):
        return "highest quality in the profile"
    threshold = profile.get("bypass_if_above_score")
    if threshold is not None and result.format_score >= int(threshold):
        return f"format score {result.format_score} >= {int(threshold)}"
    return None


def item_key(
    request_id: Optional[str], album_id: Optional[str], track_id: Optional[str], artist: str, title: str, album: Optional[str]
) -> str:
    if request_id:
        return f"req:{request_id}"
    if album_id:
        return f"album:{album_id}"
    if track_id:
        return f"track:{track_id}"
    return "name:" + "|".join(str(x or "").strip().lower() for x in (artist, title, album))


def candidate_to_dict(candidate: AcquisitionSearchResult) -> dict[str, Any]:
    return {name: getattr(candidate, name) for name in _CANDIDATE_FIELDS}


def candidate_from_dict(data: dict[str, Any]) -> AcquisitionSearchResult:
    kwargs = {k: data[k] for k in _CANDIDATE_FIELDS if k in data}
    kwargs.setdefault("download_id", "")
    kwargs.setdefault("title", "")
    kwargs.setdefault("artist", "")
    return AcquisitionSearchResult(**kwargs)


@dataclass
class GateDecision:
    grab: bool
    reason: str
    pending: Optional[dict[str, Any]] = None
    candidate: Optional[AcquisitionSearchResult] = None  # what to grab when ``grab`` (the held winner may differ)


def apply_gate(
    db: Any,
    *,
    profile: dict[str, Any],
    top_tier: Optional[int],
    candidate: AcquisitionSearchResult,
    result: EvaluationResult,
    rank: tuple[Any, ...],
    artist: str,
    item_title: str,
    album: Optional[str],
    item_type: str,
    request_id: Optional[str],
    album_id: Optional[str],
    track_id: Optional[str],
    quality_profile_id: Optional[str] = None,
    upgrade_floor: Optional[int] = None,
    now: Optional[datetime] = None,
) -> GateDecision:
    """Decides grab-now vs. hold for the best-ranked candidate of one item, maintaining ``pending_releases``."""
    now = now or utcnow()
    key = item_key(request_id, album_id, track_id, artist, item_title, album)
    existing = db.get_pending_release_by_key(key)

    delay = delay_minutes(profile, candidate.protocol)
    bypass = bypass_reason(profile, result, top_tier)
    if delay <= 0 or bypass:
        if existing:
            db.delete_pending_release(existing["id"])
        why = "no delay for this protocol" if delay <= 0 else f"delay bypassed: {bypass}"
        return GateDecision(True, why, candidate=candidate)

    added_at = parse_ts(existing["added_at"]) if existing else now
    release_at = added_at + timedelta(minutes=delay)
    rank_list = [list(x) if isinstance(x, tuple) else x for x in rank]

    if existing and existing["rank"] > rank_list:
        # The parked release still beats everything the search found now (it may have dropped out of the feed).
        return GateDecision(False, existing["reason"], pending=existing)

    if now >= release_at:
        if existing:
            db.delete_pending_release(existing["id"])
        return GateDecision(True, f"delay window of {delay} min elapsed", candidate=candidate)

    reason = f"Delayed {delay} min for {normalize_protocol(candidate.protocol)} (profile '{profile.get('name')}')"
    pending = db.upsert_pending_release(
        {
            "item_key": key,
            "title": candidate.title,
            "artist_name": artist,
            "album": album,
            "item_type": item_type,
            "album_id": album_id,
            "track_id": track_id,
            "request_id": request_id,
            "protocol": normalize_protocol(candidate.protocol),
            "quality": result.parsed_quality,
            "format_score": result.format_score,
            "payload": {
                "candidate": candidate_to_dict(candidate),
                "quality_profile_id": quality_profile_id,
                "upgrade_floor": upgrade_floor,
                "score": result.score,
            },
            "rank": rank_list,
            "delay_profile_id": profile.get("id"),
            "reason": reason,
            "added_at": _iso(added_at),
            "release_at": _iso(release_at),
        }
    )
    return GateDecision(False, reason, pending=pending)

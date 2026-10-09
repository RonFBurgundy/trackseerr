"""Delay-profile selection and the delay gate (spec sections 5 and 6, step 7).

Ranking has already picked the best candidate for an item; the gate decides whether to grab it now or park it in
``pending_releases`` until ``release_at``. As in Lidarr the window is anchored on the first time the item was seen:
a better candidate arriving inside the window replaces the pending one but never restarts the clock.

Artist tags: read from the ``artist_tags`` table (see ``tag_store``), by artist id when the caller has one, else by
library artist name. An artist with no tags (or one not in the library) uses the default delay profile.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import sqlite3
from typing import Any, Optional

from trackseerr.decision_engine import PreparedProfile, normalize_protocol
from trackseerr.item_history import GrabTrigger
from trackseerr.models import AcquisitionSearchResult, EvaluationResult

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


def artist_tags(db: Any, artist_name: Optional[str], artist_id: Optional[str] = None) -> list[str]:
    """Tag labels of a library artist, by ``artist_id`` or else by name (empty when unknown or untagged)."""
    if not artist_id and not (artist_name or "").strip():
        return []
    try:
        return db.get_artist_tag_labels(artist_id=artist_id, artist_name=artist_name)
    except sqlite3.Error as exc:
        logger.warning("Could not read artist tags for '%s': %s", artist_name or artist_id, type(exc).__name__)
        return []


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


def resolve_delay_profile(
    db: Any, artist_name: Optional[str], artist_id: Optional[str] = None, tags: Optional[list[str]] = None
) -> dict[str, Any]:
    """Delay profile for an artist; pass ``tags`` when the caller already looked them up."""
    try:
        profiles = db.list_delay_profiles()
    except sqlite3.Error as exc:
        logger.warning("Could not load delay profiles (%s); delays disabled for this decision", type(exc).__name__)
        profiles = []
    return select_delay_profile(profiles, tags if tags is not None else artist_tags(db, artist_name, artist_id))


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
    """Canonical key: ``album:`` > ``track:`` > ``req:`` > name. (Rows also store every id; see ``find_pending_releases``.)"""
    if album_id:
        return f"album:{album_id}"
    if track_id:
        return f"track:{track_id}"
    if request_id:
        return f"req:{request_id}"
    return "name:" + "|".join(str(x or "").strip().lower() for x in (artist, title, album))


def rank_beats(held: Any, new: Any) -> bool:
    """True when rank list ``held`` is strictly better than ``new``; ``None`` entries and mixed types never raise.

    ``None`` sorts below every value (a missing score is the worst), mirroring how a missing field ranks last.
    """
    held = list(held or [])
    new = list(new or [])
    for a, b in zip(held, new):
        if a == b:
            continue
        if a is None:
            return False
        if b is None:
            return True
        try:
            return bool(a > b)
        except TypeError:
            return str(a) > str(b)
    return len(held) > len(new)


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
    # Row this call claimed (removed) from the queue; if the grab then fails, ``db.restore_pending_release(claimed)``.
    claimed: Optional[dict[str, Any]] = None


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
    trigger: Optional[GrabTrigger] = None,
) -> GateDecision:
    """Decides grab-now vs. hold for the best-ranked candidate of one item, maintaining ``pending_releases``."""
    now = now or utcnow()
    key = item_key(request_id, album_id, track_id, artist, item_title, album)
    matches = db.find_pending_releases(request_id, album_id, track_id)
    if not matches:
        by_key = db.get_pending_release_by_key(key)
        matches = [by_key] if by_key else []
    existing = matches[0] if matches else None  # earliest added_at wins: the window anchor
    for extra in matches[1:]:  # duplicates of one item (legacy keys) collapse into the earliest row
        db.delete_pending_release(extra["id"])

    delay = delay_minutes(profile, candidate.protocol)
    bypass = bypass_reason(profile, result, top_tier)
    if delay <= 0 or bypass:
        claimed = None
        if existing:
            claimed = db.claim_pending_release(existing["id"])
            if claimed is None:
                return GateDecision(False, "already being released", pending=existing)
        why = "no delay for this protocol" if delay <= 0 else f"delay bypassed: {bypass}"
        return GateDecision(True, why, candidate=candidate, claimed=claimed)

    added_at = parse_ts(existing["added_at"]) if existing else now
    release_at = added_at + timedelta(minutes=delay)
    rank_list = [list(x) if isinstance(x, tuple) else x for x in rank]

    if existing and rank_beats(existing["rank"], rank_list):
        # The parked release still beats everything the search found now (it may have dropped out of the feed).
        return GateDecision(False, existing["reason"], pending=existing)

    if now >= release_at:
        claimed = None
        if existing:
            claimed = db.claim_pending_release(existing["id"])
            if claimed is None:  # the tick, the endpoint or another search is grabbing this item right now
                return GateDecision(False, "already being released", pending=existing)
        return GateDecision(True, f"delay window of {delay} min elapsed", candidate=candidate, claimed=claimed)

    reason = f"Delayed {delay} min for {normalize_protocol(candidate.protocol)} (profile '{profile.get('name')}')"
    if existing and existing["item_key"] != key:  # re-key under the canonical key, window anchor preserved
        db.delete_pending_release(existing["id"])
    pending = db.upsert_pending_release(
        {
            "item_key": key,
            "title": candidate.title,
            "artist_name": artist,
            "album": album,
            "item_type": item_type,
            "album_id": album_id or (existing or {}).get("album_id"),
            "track_id": track_id or (existing or {}).get("track_id"),
            "request_id": request_id or (existing or {}).get("request_id"),
            "protocol": normalize_protocol(candidate.protocol),
            "quality": result.parsed_quality,
            "format_score": result.format_score,
            "payload": {
                "candidate": candidate_to_dict(candidate),
                "item_title": item_title,
                "quality_profile_id": quality_profile_id,
                "upgrade_floor": upgrade_floor,
                "score": result.score,
                "trigger": trigger.to_dict() if trigger else None,  # survives the hold: the release-time grab uses it
            },
            "rank": rank_list,
            "delay_profile_id": profile.get("id"),
            "reason": reason,
            "added_at": _iso(added_at),
            "release_at": _iso(release_at),
        }
    )
    return GateDecision(False, reason, pending=pending)

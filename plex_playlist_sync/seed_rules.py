"""Per-indexer seed rules: grab-time resolution, snapshotting and client push (see docs/indexer-seed-rules.md).

Effective targets are resolved once when a release is grabbed and snapshotted onto the download row, so editing an
indexer later never changes a torrent that is already seeding. ``None`` on an indexer field means "inherit the
global limit"; ``0`` means "no requirement".
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

SEED_SOURCE_INDEXER = "indexer"
SEED_SOURCE_GLOBAL = "global"

# Release titles that name a multi-album / artist-level bundle. The search model carries no discography flag, so
# the title is the only signal at grab time.
_DISCOGRAPHY_RE = re.compile(
    r"discograph|\bcomplete\s+(?:studio\s+)?(?:albums?|works|collection|recordings|catalog(?:ue)?)\b|\bthe\s+complete\b"
    r"|\b\d{1,3}\s*(?:albums|cds)\b",
    re.IGNORECASE,
)


def is_discography_release(title: Optional[str], extra: Optional[dict[str, Any]] = None) -> bool:
    """True when a release looks like a multi-album bundle (explicit ``extra['is_discography']`` wins)."""
    if isinstance(extra, dict) and extra.get("is_discography") is not None:
        return bool(extra["is_discography"])
    return bool(title and _DISCOGRAPHY_RE.search(title))


def resolve_seed_targets(
    indexer: Optional[dict[str, Any]], media_settings: dict[str, Any], is_discography: bool = False
) -> tuple[Optional[float], Optional[int], Optional[str]]:
    """Returns ``(ratio_target, time_target_minutes, source)``.

    Each field takes the indexer value when it is not NULL, else the global limit. A discography grab prefers
    ``discography_seed_time_minutes``. ``source`` is ``'indexer'`` if any indexer field supplied a value,
    ``'global'`` if only global limits apply, and ``None`` when there are no limits at all.
    """
    idx = indexer or {}
    from_indexer = False

    ratio = idx.get("seed_ratio")
    if ratio is not None:
        from_indexer = True
        ratio = float(ratio)
    else:
        g = media_settings.get("seed_ratio_limit")
        ratio = float(g) if g is not None else None

    minutes: Any = None
    if is_discography and idx.get("discography_seed_time_minutes") is not None:
        minutes = idx["discography_seed_time_minutes"]
        from_indexer = True
    elif idx.get("seed_time_minutes") is not None:
        minutes = idx["seed_time_minutes"]
        from_indexer = True
    else:
        minutes = media_settings.get("seed_time_limit_minutes")
    minutes = int(minutes) if minutes is not None else None

    if from_indexer:
        return ratio, minutes, SEED_SOURCE_INDEXER
    if ratio is None and minutes is None:
        return None, None, None
    return ratio, minutes, SEED_SOURCE_GLOBAL


def apply_seed_rules_at_grab(
    db: Any,
    driver: Any,
    download_id: str,
    download_hash: Optional[str],
    title: str,
    protocol: Optional[str],
    extra: Optional[dict[str, Any]],
) -> None:
    """Snapshots the indexer id + effective seed targets onto a fresh download and pushes indexer limits to the client.

    Torrent grabs only. Never raises: a failure here must not undo a grab that already reached the client.
    """
    if str(protocol or "").lower() != "torrent":
        return
    extra = extra if isinstance(extra, dict) else {}
    indexer_id = str(extra["indexer_id"]) if extra.get("indexer_id") else None
    try:
        indexer = db.get_indexer(indexer_id) if indexer_id else None
        media_settings = db.get_media_management_settings()
        ratio, minutes, source = resolve_seed_targets(
            indexer, media_settings, is_discography_release(title, extra)
        )
        db.set_download_seed_rule(download_id, indexer_id, ratio, minutes, source)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        logger.warning("Could not snapshot seed rules for %s: %s", download_id, exc)
        return
    # Global limits keep today's behaviour (TrackSeerr governs them); only indexer rules are mirrored to the client.
    if source != SEED_SOURCE_INDEXER or not download_hash:
        return
    try:
        driver.set_share_limits(download_hash, ratio, minutes)
    except Exception as exc:  # driver errors span HTTP, auth and parsing; the grab itself already succeeded
        logger.warning("Could not push share limits for %s to the client: %s", download_id, exc)


def seed_rule_conflict(import_mode: Optional[str], indexers: list[dict[str, Any]]) -> bool:
    """True when ``move`` import mode would defeat a seed rule: any enabled torrent (torznab) indexer has a
    non-null, non-zero ratio, seed time or discography seed time."""
    if str(import_mode or "").lower() != "move":
        return False
    for idx in indexers:
        if not idx.get("enabled", True) or str(idx.get("indexer_type") or "torznab").lower() != "torznab":
            continue
        for key in ("seed_ratio", "seed_time_minutes", "discography_seed_time_minutes"):
            if idx.get(key):
                return True
    return False

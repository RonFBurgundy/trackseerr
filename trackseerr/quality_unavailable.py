"""Notice admin when an item only exists in disallowed qualities.

When an item has been searched repeatedly (>= QUALITY_UNAVAILABLE_AFTER times) and
available candidates are rejected solely because their quality is disallowed by
the active quality profile, emit an item event and notify the administrator.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Sequence
from typing import Any

from trackseerr.decision_engine import (
    DurationInfo,
    candidate_context,
    evaluate_prepared,
    prepare_profile,
)
from trackseerr.item_history import emit, emit_named
from trackseerr.models import NotificationEvent, QualityProfile
from trackseerr.notifications import notification_dispatcher
from trackseerr.quality import parse_release_title

logger = logging.getLogger(__name__)

QUALITY_UNAVAILABLE_AFTER = 5


def disallowed_qualities(
    candidates: Sequence[Any],
    profile: QualityProfile | dict[str, Any],
    *,
    duration: DurationInfo | None = None,
    artist_tags: Iterable[str] | None = None,
) -> list[str]:
    """Return sorted unique list of parsed qualities for candidates rejected solely due to disallowed quality."""
    if isinstance(profile, dict):
        from trackseerr.acquisition_coordinator import _to_quality_profile

        prof = _to_quality_profile(profile)
    else:
        prof = profile

    prepared = prepare_profile(prof)
    qualities: set[str] = set()

    for c in candidates:
        parsed = parse_release_title(c.title)
        size = getattr(c, "size_bytes", None)
        size_bytes = size if size and size > 0 else None
        eval_res = evaluate_prepared(
            parsed,
            prepared,
            size_bytes,
            duration=duration,
            artist_tags=artist_tags,
            **candidate_context(c),
        )
        if eval_res.breakdown and eval_res.breakdown.rejections:
            codes = {r.get("code") for r in eval_res.breakdown.rejections if isinstance(r, dict)}
            if codes == {"quality_not_allowed"} and eval_res.parsed_quality:
                qualities.add(eval_res.parsed_quality)

    return sorted(qualities)


def maybe_notify(
    db: Any,
    *,
    candidates: Sequence[Any],
    profile: QualityProfile | dict[str, Any],
    profile_name: str,
    artist: str | None = None,
    title: str | None = None,
    track_id: str | None = None,
    album_id: str | None = None,
    request_id: str | None = None,
) -> bool:
    """Check if lower quality is available and notify admin if threshold met and not notified yet."""
    if not (track_id or album_id or request_id):
        return False

    qualities = disallowed_qualities(candidates, profile)
    if not qualities:
        return False

    searches = db.count_item_searches(track_id=track_id, album_id=album_id, request_id=request_id)
    if searches < QUALITY_UNAVAILABLE_AFTER:
        return False

    if db.item_event_count("quality_unavailable", track_id=track_id, album_id=album_id, request_id=request_id) > 0:
        return False

    qualities_str = ", ".join(qualities)
    body = (
        f"Searched {searches} times. Only {qualities_str} found, which the '{profile_name}' profile does not allow. "
        f"Keep waiting, or allow these formats in the profile."
    )
    details = {
        "qualities": qualities,
        "searches": searches,
        "profile": profile_name,
    }

    try:
        if track_id or album_id:
            emit(
                db,
                "quality_unavailable",
                track_id=track_id,
                album_id=album_id,
                request_id=request_id,
                message=body,
                details=details,
            )
        else:
            emit_named(
                db,
                "quality_unavailable",
                artist=artist,
                title=title,
                request_id=request_id,
                message=body,
                details=details,
            )
    except (sqlite3.Error, ValueError) as e:
        logger.warning("Failed to emit quality_unavailable item event: %s", e)

    try:
        db.record_event(
            "quality_unavailable",
            body,
            source="AcquisitionCoordinator",
            severity="warning",
        )
    except (sqlite3.Error, ValueError) as e:
        logger.warning("Failed to record quality_unavailable system event: %s", e)

    try:
        notification_dispatcher.dispatch(
            NotificationEvent.QUALITY_UNAVAILABLE,
            data={
                "artist": artist,
                "title": title,
                "qualities": qualities_str,
                "searches": searches,
                "profile": profile_name,
                "request_id": request_id,
            },
            db=db,
        )
    except (sqlite3.Error, ValueError) as e:
        logger.warning("Failed to dispatch quality_unavailable notification: %s", e)

    return True

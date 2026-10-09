"""Shared constants and helper functions for Trackseerr storage."""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

def clean_library_name(text: str) -> str:
    """Normalizes string for indexing and resilient comparison: lowercased, alphanumerics and single spaces.

    '_' counts as whitespace: iTunes writes it in folder names in place of characters illegal on Windows
    ('Daft Punk_ Pharrell Williams' for the tag 'Daft Punk; Pharrell Williams').
    """
    if not text:
        return ""
    cleaned = re.sub(r"[^\w\s]", "", str(text).lower().replace("_", " "))
    return re.sub(r"\s+", " ", cleaned).strip()


_NEAR_TITLE_RATIO = 0.8  # title similarity that lets a matching track number confirm "same track"
_TRACK_DURATION_TOLERANCE = 2.0  # seconds: durations this close count as the same recording when merging tracks
SEED_COMPLETE_ACTIONS = ("keep", "remove", "remove_and_delete")
SCHEMA_VERSION = 71  # head of the migration list in Database._migrate; bump with every new migration (tests import it)


def _opt_float(value: Any) -> Optional[float]:
    """NULL-preserving float coercion for nullable numeric columns."""
    return None if value is None or value == "" else float(value)


def _opt_int(value: Any) -> Optional[int]:
    """NULL-preserving int coercion for nullable numeric columns."""
    return None if value is None or value == "" else int(value)


def _titles_near_equal(a: str, b: str) -> bool:
    """True when two clean titles plausibly name the same track (a track number then breaks the tie).

    Titles that differ only in digits ('Intro 2' / 'Intro 3', 'Part 1' / 'Part 2') are different tracks.
    """
    if not a or not b:
        return False
    if a != b and re.sub(r"\d", "", a) == re.sub(r"\d", "", b):
        return False
    shorter, longer = sorted((a, b), key=len)
    if len(shorter) >= 4 and shorter in longer:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= _NEAR_TITLE_RATIO
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_KNOWN_PERMISSION_MASK = 1 | 2 | 4 | 8 | 16 | 32 | 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _us_of(dt: datetime) -> int:
    """Epoch microseconds of an aware datetime."""
    return (dt - _EPOCH) // timedelta(microseconds=1)


def _now_us() -> int:
    return _us_of(_utcnow())


def ts_to_us(value: Any) -> int:
    """Epoch microseconds of an ISO / SQLite timestamp string (naive values are UTC); 0 when unset or unparseable."""
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return _us_of(dt)


RESERVED_USER_IDS = frozenset({"", "0", "1", "api_key_user", "gateway_service", "internal_gateway"})


logger = logging.getLogger("trackseerr.storage")

# How long a statement waits for a competing writer before SQLite raises "database is locked".
_BUSY_TIMEOUT_MS = 15_000

_LEGACY_LIDARR_OVERRIDE_COLUMNS = ("monitor_option", "quality_profile_id", "metadata_profile_id", "tag_ids")

# Missing-track statuses that are retried on a schedule instead of on every trickle pass.
LIDARR_BACKOFF_STATUSES = ("error", "rate_limited")
LIDARR_WEEKLY_STATUSES = ("unavailable", "not_found")
LIDARR_WEEKLY_RETRY = timedelta(days=7)
LIDARR_ERROR_BACKOFF = (timedelta(hours=1), timedelta(hours=6), timedelta(hours=24))
_RETRY_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def lidarr_retry_delay(status: str, attempts: int) -> Optional[timedelta]:
    """How long to wait before re-sending an item whose Lidarr outcome was ``status`` after ``attempts`` tries.

    ``unavailable`` (not in the metadata profile) and ``not_found`` are re-checked weekly; ``error`` and
    ``rate_limited`` back off 1h, 6h, 24h and then weekly. Other statuses are not scheduled (``None``).
    """
    if status in LIDARR_WEEKLY_STATUSES:
        return LIDARR_WEEKLY_RETRY
    if status in LIDARR_BACKOFF_STATUSES:
        index = max(1, int(attempts)) - 1
        return LIDARR_ERROR_BACKOFF[index] if index < len(LIDARR_ERROR_BACKOFF) else LIDARR_WEEKLY_RETRY
    return None


# Retry schedule for approved requests whose Lidarr outcome left them stuck (see ``Database.set_request_outcome``).
REQUEST_RETRY_BACKOFF: dict[str, tuple[timedelta, ...]] = {
    "albums_pending": (timedelta(minutes=2), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6)),
    "rate_limited": (timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2)),
    "monitor_failed": (timedelta(minutes=15), timedelta(hours=1), timedelta(hours=6)),
}
REQUEST_RETRY_TAIL = timedelta(hours=24)
REQUEST_FAST_RETRY_REASONS = tuple(REQUEST_RETRY_BACKOFF)


def request_retry_delay(reason: str, attempts: int, retry_after: Optional[float] = None) -> Optional[timedelta]:
    """Delay before re-sending a request stuck with ``reason`` after ``attempts`` tries (``None`` = not retried).

    ``not_in_metadata_profile`` is re-checked weekly like a missing track. For ``rate_limited`` a known
    ``retry_after`` (seconds) is honoured when it is longer than the scheduled delay.
    """
    if reason == "not_in_metadata_profile":
        return LIDARR_WEEKLY_RETRY
    steps = REQUEST_RETRY_BACKOFF.get(reason)
    if steps is None:
        return None
    index = max(1, int(attempts)) - 1
    delay = steps[index] if index < len(steps) else REQUEST_RETRY_TAIL
    if reason == "rate_limited" and retry_after and retry_after > 0:
        delay = max(delay, timedelta(seconds=float(retry_after)))
    return delay


def lidarr_item_due(row: dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when a missing track should be (re)sent to Lidarr now: not monitored and its retry time has passed."""
    if row.get("lidarr_status") == "monitored":
        return False
    due_at = row.get("next_attempt_at")
    if not due_at:
        return True
    current = (now or datetime.now(timezone.utc)).strftime(_RETRY_TS_FORMAT)
    return str(due_at) <= current


# ``download_history.message`` of a ``grabbed`` row made by an issue replacement search (followed by the issue id).
REPLACEMENT_MESSAGE_PREFIX = "Replacement search for issue "


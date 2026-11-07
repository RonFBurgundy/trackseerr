"""In-memory indexer backoff and status tracking.

Pauses failing indexers for escalating backoff periods when searches fail,
honoring HTTP 429 Retry-After and Newznab daily API limits (codes 500/501).

State is kept in-memory and resets on process restart.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from trackseerr.clients.acquisition.torznab import IndexerRateLimited
from trackseerr.redaction import redact_text

logger = logging.getLogger(__name__)

BACKOFF_STEPS_SECONDS: tuple[int, ...] = (
    0,
    60,
    300,
    900,
    1800,
    3600,
    10800,
    21600,
    43200,
    86400,
)

API_LIMIT_MIN_SECONDS: int = 3600


@dataclass
class IndexerState:
    failures: int
    paused_until: datetime | None
    last_error: str


class IndexerStatusRegistry:
    """Thread-safe registry tracking failure counts and backoff state per indexer."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, IndexerState] = {}

    def record_success(self, indexer_id: str) -> None:
        """Clear failure state for an indexer following a successful search."""
        if not indexer_id:
            return
        with self._lock:
            self._states.pop(str(indexer_id), None)

    def record_failure(
        self,
        indexer_id: str,
        exc: BaseException,
        now: datetime | None = None,
    ) -> IndexerState:
        """Record a failure for an indexer, recalculating pause duration."""
        idx_id = str(indexer_id)
        if now is None:
            ref_now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            ref_now = now.replace(tzinfo=timezone.utc)
        else:
            ref_now = now.astimezone(timezone.utc)

        with self._lock:
            prev = self._states.get(idx_id)
            prev_failures = prev.failures if prev else 0
            failures = prev_failures + 1

            idx = min(failures - 1, len(BACKOFF_STEPS_SECONDS) - 1)
            wait: float = float(BACKOFF_STEPS_SECONDS[idx])

            if isinstance(exc, IndexerRateLimited):
                retry_val = float(exc.retry_after) if exc.retry_after is not None else 0.0
                floor_val = (
                    float(API_LIMIT_MIN_SECONDS)
                    if (exc.code in (500, 501) and exc.retry_after is None)
                    else 60.0
                )
                wait = max(wait, retry_val, floor_val)

            paused_time = (
                ref_now + timedelta(seconds=wait) if wait > 0 else None
            )
            last_error = redact_text(str(exc))

            new_state = IndexerState(
                failures=failures,
                paused_until=paused_time,
                last_error=last_error,
            )
            self._states[idx_id] = new_state

            was_paused = (
                prev is not None
                and prev.paused_until is not None
                and prev.paused_until > ref_now
            )
            if paused_time is not None and not was_paused:
                logger.warning(
                    "Indexer '%s' paused until %s UTC after %d failure(s)",
                    idx_id,
                    paused_time.strftime("%Y-%m-%d %H:%M:%S"),
                    failures,
                )

            return IndexerState(
                failures=new_state.failures,
                paused_until=new_state.paused_until,
                last_error=new_state.last_error,
            )

    def paused_until(
        self, indexer_id: str, now: datetime | None = None
    ) -> datetime | None:
        """Return the unpause time if the indexer is currently paused, else None."""
        if not indexer_id:
            return None
        idx_id = str(indexer_id)
        if now is None:
            ref_now = datetime.now(timezone.utc)
        elif now.tzinfo is None:
            ref_now = now.replace(tzinfo=timezone.utc)
        else:
            ref_now = now.astimezone(timezone.utc)

        with self._lock:
            state = self._states.get(idx_id)
            if state is None or state.paused_until is None:
                return None
            if state.paused_until > ref_now:
                return state.paused_until
            return None

    def snapshot(self, indexer_id: str) -> IndexerState | None:
        """Return a copy of the current state for an indexer, if any."""
        if not indexer_id:
            return None
        with self._lock:
            state = self._states.get(str(indexer_id))
            if state is None:
                return None
            return IndexerState(
                failures=state.failures,
                paused_until=state.paused_until,
                last_error=state.last_error,
            )

    def reset(self) -> None:
        """Reset all tracked indexer states (intended for testing)."""
        with self._lock:
            self._states.clear()


indexer_status = IndexerStatusRegistry()

__all__ = [
    "API_LIMIT_MIN_SECONDS",
    "BACKOFF_STEPS_SECONDS",
    "IndexerState",
    "IndexerStatusRegistry",
    "indexer_status",
]
